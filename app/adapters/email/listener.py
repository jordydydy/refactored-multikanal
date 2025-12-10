import imaplib
import email
import time
import requests
import logging
import msal
from email.header import decode_header
from typing import Dict, Any, Optional, Set

from app.core.config import settings
from app.adapters.email.utils import sanitize_email_body
from app.repositories.message import MessageRepository

logger = logging.getLogger("email.listener")
repo = MessageRepository()

_token_cache: Dict[str, Any] = {}

# [FIX] Memory Cache untuk mencegah Race Condition (Double Processing)
# Menyimpan ID pesan yang sedang diproses dalam runtime ini
_processing_cache: Set[str] = set()

def get_graph_token() -> Optional[str]:
    global _token_cache
    if _token_cache and _token_cache.get("expires_at", 0) > time.time() + 60:
        return _token_cache.get("access_token")

    if not all([settings.AZURE_CLIENT_ID, settings.AZURE_CLIENT_SECRET, settings.AZURE_TENANT_ID]):
        logger.error("Azure credentials not fully configured for Listener.")
        return None

    try:
        app = msal.ConfidentialClientApplication(
            settings.AZURE_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{settings.AZURE_TENANT_ID}",
            client_credential=settings.AZURE_CLIENT_SECRET,
        )
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" in result:
            _token_cache = {
                "access_token": result["access_token"],
                "expires_at": time.time() + result.get("expires_in", 3500)
            }
            logger.info("New Azure OAuth2 token acquired for Listener.")
            return result["access_token"]
        else:
            logger.error(f"Failed to acquire Graph token: {result.get('error_description')}")
            return None
    except Exception as e:
        logger.error(f"Azure Auth Exception in Listener: {e}")
        return None

def decode_str(header_val):
    if not header_val: return ""
    decoded_list = decode_header(header_val)
    text = ""
    for content, encoding in decoded_list:
        if isinstance(content, bytes):
            text += content.decode(encoding or "utf-8", errors="ignore")
        else:
            text += str(content)
    return text

def process_single_email(sender_email, sender_name, subject, body, msg_id, references, thread_key, graph_message_id=None):
    if "mailer-daemon" in sender_email.lower() or "noreply" in sender_email.lower():
        return

    payload = {
        "platform_unique_id": sender_email,
        "query": body,
        "platform": "email",
        "metadata": {
            "subject": subject,
            "in_reply_to": msg_id,
            "references": references,
            "sender_name": sender_name,
            "thread_key": thread_key,
            "graph_message_id": graph_message_id
        }
    }
    
    try:
        api_url = f"http://127.0.0.1:9798/api/messages/process" 
        requests.post(api_url, json=payload, timeout=10)
        logger.info(f"Email from {sender_email} processed. Thread: {thread_key}")
    except Exception as req_err:
        logger.error(f"Failed to push email to API: {req_err}")

def _poll_graph_api():
    token = get_graph_token()
    if not token: return

    user_id = settings.AZURE_EMAIL_USER
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/mailFolders/inbox/messages"
    params = {
        "$filter": "isRead eq false",
        "$top": 10,
        "$select": "id,subject,from,body,internetMessageId,conversationId,isRead,internetMessageHeaders"
    }
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=20)
        if resp.status_code != 200:
            logger.error(f"Graph API Error {resp.status_code}: {resp.text}")
            return

        messages = resp.json().get("value", [])
        if messages:
            logger.info(f"Found {len(messages)} new emails via Graph API.")

        for msg in messages:
            graph_id = msg.get("id")
            msg_id = msg.get("internetMessageId", "").strip()
            
            # [FIX] Deduplikasi Level 1: Memory Check
            if msg_id in _processing_cache:
                logger.info(f"Skipping duplicate in-memory: {msg_id}")
                continue

            # [FIX] Deduplikasi Level 2: Database Check
            if repo.is_processed(msg_id, "email"):
                _mark_graph_read(user_id, graph_id, token)
                continue
            
            # Masukkan ke cache memori agar tidak diproses ulang di loop ini
            _processing_cache.add(msg_id)

            # Batasi ukuran cache agar RAM aman
            if len(_processing_cache) > 1000:
                _processing_cache.clear()

            subject = msg.get("subject", "")
            sender_info = msg.get("from", {}).get("emailAddress", {})
            sender_email = sender_info.get("address", "")
            sender_name = sender_info.get("name", "")
            
            body_content = msg.get("body", {}).get("content", "")
            body_type = msg.get("body", {}).get("contentType", "Text")
            
            if body_type.lower() == "html":
                clean_body = sanitize_email_body(None, body_content)
            else:
                clean_body = sanitize_email_body(body_content, None)

            if not clean_body: 
                _mark_graph_read(user_id, graph_id, token)
                continue

            headers_list = msg.get("internetMessageHeaders", []) or []
            references = ""
            in_reply_to = ""
            for h in headers_list:
                h_name = h.get("name", "").lower()
                if h_name == "references":
                    references = h.get("value", "")
                elif h_name == "in-reply-to":
                    in_reply_to = h.get("value", "")

            if references:
                thread_key = references.split()[0].strip()
            elif in_reply_to:
                thread_key = in_reply_to.strip()
            else:
                thread_key = msg_id

            process_single_email(sender_email, sender_name, subject, clean_body, msg_id, references, thread_key, graph_id)
            
            _mark_graph_read(user_id, graph_id, token)

    except Exception as e:
        logger.error(f"Graph Polling Exception: {e}")

def _mark_graph_read(user_id, message_id, token):
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/messages/{message_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    try:
        requests.patch(url, json={"isRead": True}, headers=headers, timeout=5)
    except Exception:
        pass

def _poll_imap():
    try:
        mail = imaplib.IMAP4_SSL(settings.EMAIL_HOST, settings.EMAIL_PORT)
        mail.login(settings.EMAIL_USER, settings.EMAIL_PASS)
        mail.select("INBOX")
        
        status, messages = mail.search(None, 'UNSEEN')
        email_ids = messages[0].split()
        
        if email_ids:
            logger.info(f"Found {len(email_ids)} new emails via IMAP.")
            
        for e_id in email_ids:
            _, msg_data = mail.fetch(e_id, '(RFC822)')
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            
            msg_id = msg.get("Message-ID", "").strip()
            
            # Deduplikasi Memory & DB
            if msg_id in _processing_cache: continue
            if repo.is_processed(msg_id, "email"): continue
            
            _processing_cache.add(msg_id)
            if len(_processing_cache) > 1000: _processing_cache.clear()
            
            subject = decode_str(msg.get("Subject"))
            sender = decode_str(msg.get("From"))
            sender_email = sender.split('<')[-1].replace('>', '').strip() if '<' in sender else sender
            sender_name = sender.split('<')[0].strip()

            text_plain, html = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        text_plain = part.get_payload(decode=True).decode(errors='ignore')
                    elif part.get_content_type() == "text/html":
                        html = part.get_payload(decode=True).decode(errors='ignore')
            else:
                text_plain = msg.get_payload(decode=True).decode(errors='ignore')

            clean_body = sanitize_email_body(text_plain, html)
            if not clean_body: continue

            references = msg.get("References", "")
            in_reply_to = msg.get("In-Reply-To", "")
            
            if references:
                thread_key = references.split()[0].strip()
            elif in_reply_to:
                thread_key = in_reply_to.strip()
            else:
                thread_key = msg_id

            process_single_email(sender_email, sender_name, subject, clean_body, msg_id, references, thread_key, None)

        mail.close()
        mail.logout()

    except Exception as e:
        logger.error(f"IMAP Loop Error: {e}")

def start_email_listener():
    if not settings.EMAIL_USER:
        logger.warning("Email credentials not set. Listener stopped.")
        return

    logger.info(f"Starting Email Listener for provider: {settings.EMAIL_PROVIDER}...")
    
    while True:
        if settings.EMAIL_PROVIDER == "azure_oauth2":
            _poll_graph_api()
        else:
            _poll_imap()
        time.sleep(settings.EMAIL_POLL_INTERVAL_SECONDS)