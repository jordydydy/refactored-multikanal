import httpx
import time
from datetime import datetime, timezone
from app.core.config import settings
from app.schemas.models import ChatbotResponse
import logging

logger = logging.getLogger("service.chatbot")

class ChatbotClient:
    async def ask(self, query: str, conversation_id: str, platform: str, user_id: str) -> ChatbotResponse:
        start_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        
        safe_conv_id = conversation_id or ""

        payload = {
            "query": query,
            "platform": platform,
            "platform_unique_id": user_id,
            "conversation_id": safe_conv_id,
            "start_timestamp": start_timestamp 
        }
        
        headers = {"Content-Type": "application/json"}
        if settings.CHATBOT_API_KEY:
            headers["X-API-Key"] = settings.CHATBOT_API_KEY

        url = settings.CHATBOT_URL
        
        logger.info(f"DEBUG: Mengirim ke {url} | Timestamp: {start_timestamp} | ConvID: {safe_conv_id}")
        
        try:
            async with httpx.AsyncClient(timeout=settings.CHATBOT_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=payload, headers=headers)
                
            if resp.status_code != 200:
                logger.warning(f"Chatbot API Error {resp.status_code}: {resp.text}")
                return ChatbotResponse(success=False, answer="Mohon maaf, AI sedang sibuk.")

            data = resp.json().get("data", {})
            
            return ChatbotResponse(
                success=True,
                answer=data.get("answer"),
                conversation_id=data.get("conversation_id"),
                raw=resp.json()
            )

        except Exception as e:
            logger.error(f"Failed to call Chatbot API: {e}")
            return ChatbotResponse(success=False, answer="Mohon maaf, AI sedang sibuk (Connection Error).")