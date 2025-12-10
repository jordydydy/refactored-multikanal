import asyncio
import httpx
from typing import Dict
from app.schemas.models import IncomingMessage
from app.repositories.conversation import ConversationRepository
from app.repositories.message import MessageRepository
from app.services.chatbot import ChatbotClient
from app.adapters.base import BaseAdapter
from app.core.config import settings
import logging

logger = logging.getLogger("service.orchestrator")

class MessageOrchestrator:
    def __init__(
        self, 
        repo_conv: ConversationRepository,
        repo_msg: MessageRepository,
        chatbot: ChatbotClient,
        adapters: Dict[str, BaseAdapter]
    ):
        self.repo_conv = repo_conv
        self.repo_msg = repo_msg
        self.chatbot = chatbot
        self.adapters = adapters

    async def timeout_session(self, conversation_id: str, platform: str, user_id: str):
        adapter = self.adapters.get(platform)
        if not adapter: return

        logger.info(f"TIMEOUT: Auto-closing session {conversation_id} for {platform} user {user_id}")

        try:
            await self.chatbot.ask(
                query="Terima Kasih", 
                conversation_id=conversation_id, 
                platform=platform, 
                user_id=user_id
            )
        except Exception as e:
            logger.error(f"Failed to send close signal to AI: {e}")

        closing_text = (
            "Untuk keamanan dan kenyamanan Anda, sesi ini telah diakhiri. "
            "Silakan mulai percakapan kembali dari awal jika membutuhkan bantuan.\n\n"
            "_For your safety and convenience, I've ended this session. "
            "Feel free to start a new chat whenever you're ready._"
        )
        
        send_kwargs = {}
        if platform == "email":
            send_kwargs = {"subject": "Session Ended"}
            meta = self.repo_msg.get_email_metadata(conversation_id)
            if meta:
                send_kwargs.update(meta)
                if 'graph_message_id' in send_kwargs:
                    del send_kwargs['graph_message_id']

        adapter.send_message(user_id, closing_text, **send_kwargs)

    async def handle_feedback(self, msg: IncomingMessage):
        payload_str = msg.metadata.get("payload", "")
        if "-" not in payload_str: return
        try:
            feedback_type_raw, answer_id_raw = payload_str.split("-", 1)
        except ValueError: return
        is_good = "good" in feedback_type_raw.lower()
        session_id = msg.conversation_id or self.repo_conv.get_latest_id(msg.platform_unique_id, msg.platform)
        if not session_id: return
        backend_payload = {
            "session_id": session_id,
            "feedback": is_good,
            "answer_id": int(answer_id_raw) if answer_id_raw.isdigit() else 0
        }
        url = settings.FEEDBACK_API_URL
        headers = {"Content-Type": "application/json"}
        if settings.CORE_API_KEY: headers["X-API-Key"] = settings.CORE_API_KEY
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, json=backend_payload, headers=headers)
        except Exception as e:
            logger.error(f"Gagal kirim feedback: {e}")

    async def send_manual_message(self, data: dict):
        payload = data.get("data") if "data" in data else data
        user_id = payload.get("user") or payload.get("platform_unique_id") or payload.get("recipient_id")
        platform = payload.get("platform")
        answer = payload.get("answer") or payload.get("message")
        conversation_id = payload.get("conversation_id")
        answer_id = payload.get("answer_id")
        if not user_id or not answer or not platform: return
        adapter = self.adapters.get(platform)
        if not adapter: return
        send_kwargs = {}
        if platform == "email":
            meta = self.repo_msg.get_email_metadata(conversation_id) if conversation_id else None
            if meta: send_kwargs = meta
            else: send_kwargs = {"subject": "Re: Your Inquiry"}
        adapter.send_message(user_id, answer, **send_kwargs)
        if answer_id: adapter.send_feedback_request(user_id, answer_id)

    async def process_message(self, msg: IncomingMessage):
        adapter = self.adapters.get(msg.platform)
        if not adapter: return

        try:
            msg_id = msg.metadata.get("message_id") if msg.metadata else None
            adapter.send_typing_on(msg.platform_unique_id, message_id=msg_id)
        except Exception:
            pass

        if not msg.conversation_id:
            if msg.platform == "email" and msg.metadata and msg.metadata.get("thread_key"):
                thread_key = msg.metadata.get("thread_key")
                existing_id = self.repo_msg.get_conversation_by_thread(thread_key)
                if existing_id:
                    msg.conversation_id = existing_id
                else:
                    logger.info(f"NEW THREAD DETECTED: '{thread_key}'. Starting new session.")
            
            if not msg.conversation_id and msg.platform != "email":
                msg.conversation_id = self.repo_conv.get_active_id(msg.platform_unique_id, msg.platform)

        try:
            response = await self.chatbot.ask(msg.query, msg.conversation_id, msg.platform, msg.platform_unique_id)
        except Exception as e:
            logger.error(f"Critical error during chatbot processing: {e}")
            response = None

        try:
            adapter.send_typing_off(msg.platform_unique_id)
        except Exception: pass

        if not response or not response.answer:
            return 

        send_kwargs = {}
        if msg.platform == "email":
            if msg.metadata:
                send_kwargs = {
                    "subject": msg.metadata.get("subject"),
                    "in_reply_to": msg.metadata.get("in_reply_to"),
                    "references": msg.metadata.get("references"),
                    "graph_message_id": msg.metadata.get("graph_message_id")
                }
            else:
                meta = self.repo_msg.get_email_metadata(response.conversation_id or msg.conversation_id)
                if meta: send_kwargs = meta

        adapter.send_message(msg.platform_unique_id, response.answer, **send_kwargs)

        raw_data = response.raw.get("data", {}) if response.raw else {}
        answer_id = raw_data.get("answer_id")
        if answer_id:
            adapter.send_feedback_request(msg.platform_unique_id, answer_id)
            
        if msg.platform == "email" and response.conversation_id and msg.metadata:
            current_thread_session = self.repo_msg.get_conversation_by_thread(msg.metadata.get("thread_key"))
            if not current_thread_session or current_thread_session == response.conversation_id:
                self.repo_msg.save_email_metadata(
                    response.conversation_id,
                    msg.metadata.get("subject", ""),
                    msg.metadata.get("in_reply_to", ""),
                    msg.metadata.get("references", ""),
                    msg.metadata.get("thread_key", "")
                )