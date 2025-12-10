import smtplib
import requests
import logging
import time
import re
import msal
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import make_msgid
from typing import Optional, Dict, Any

from app.core.config import settings
from app.adapters.base import BaseAdapter

logger = logging.getLogger("adapters.email")

class EmailAdapter(BaseAdapter):
    _token_cache: Dict[str, Any] = {}

    def send_typing_on(self, recipient_id: str):
        pass 

    def send_typing_off(self, recipient_id: str):
        pass

    def _convert_markdown_to_html(self, text: str) -> str:
        text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
        text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
        text = re.sub(r'_(.*?)_', r'<i>\1</i>', text)
        return text

    def _get_graph_token(self) -> Optional[str]:
        if self._token_cache and self._token_cache.get("expires_at", 0) > time.time() + 60:
            return self._token_cache.get("access_token")

        if not all([settings.AZURE_CLIENT_ID, settings.AZURE_CLIENT_SECRET, settings.AZURE_TENANT_ID]):
            logger.error("Azure credentials not fully configured.")
            return None

        try:
            app = msal.ConfidentialClientApplication(
                settings.AZURE_CLIENT_ID,
                authority=f"https://login.microsoftonline.com/{settings.AZURE_TENANT_ID}",
                client_credential=settings.AZURE_CLIENT_SECRET,
            )
            result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
            if "access_token" in result:
                self._token_cache = {
                    "access_token": result["access_token"],
                    "expires_at": time.time() + result.get("expires_in", 3500)
                }
                logger.info("New Azure OAuth2 token acquired.")
                return result["access_token"]
            else:
                logger.error(f"Failed to acquire Graph token: {result.get('error_description')}")
                return None
        except Exception as e:
            logger.error(f"Azure Auth Exception: {e}")
            return None

    def send_message(self, recipient_id: str, text: str, **kwargs):
        subject = kwargs.get("subject", "Re: Your Inquiry")
        in_reply_to = kwargs.get("in_reply_to")
        references = kwargs.get("references")
        # [FIX] Ambil graph_message_id yang dikirim Orchestrator
        graph_message_id = kwargs.get("graph_message_id")
        
        text = self._convert_markdown_to_html(text)
        html_body = text.replace('\n', '<br>')
        
        formatted_body = (
            f"Dear Bapak/Ibu,<br><br>{html_body}<br><br>"
            "Regards,<br>Kementerian Investasi dan Hilirisasi/BKPM"
        )

        if settings.EMAIL_PROVIDER == "azure_oauth2":
            return self._send_via_graph(recipient_id, subject, formatted_body, graph_message_id)
        else:
            return self._send_via_smtp(recipient_id, subject, formatted_body, in_reply_to, references)

    def _send_via_graph(self, to_email: str, subject: str, html_body: str, graph_message_id: str = None):
        """
        Mengirim email via Graph API.
        - Jika ada 'graph_message_id', gunakan endpoint '/reply' (Threading Native).
        - Jika tidak ada, gunakan 'sendMail' (New Thread).
        """
        token = self._get_graph_token()
        if not token:
            return {"sent": False, "error": "Could not acquire Azure token"}

        user_id = settings.AZURE_EMAIL_USER
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        # [LOGIKA 1: REPLY THREADING]
        # Endpoint ini yang akan memaksa email masuk ke thread yang sama!
        if graph_message_id:
            logger.info(f"Replying to existing thread using Graph ID: {graph_message_id}")
            url = f"https://graph.microsoft.com/v1.0/users/{user_id}/messages/{graph_message_id}/reply"
            payload = {
                "comment": html_body  # Hanya perlu body baru
            }
            try:
                response = requests.post(url, json=payload, headers=headers, timeout=10)
                if response.status_code == 202:
                    return {"sent": True, "method": "azure_graph_reply"}
                else:
                    logger.warning(f"Graph Reply Failed ({response.status_code}). Fallback to sendMail.")
            except Exception as e:
                logger.error(f"Graph Reply Exception: {e}")

        # [LOGIKA 2: SEND NEW MAIL] (Fallback / Default)
        url = f"https://graph.microsoft.com/v1.0/users/{user_id}/sendMail"
        email_msg = {
            "message": {
                "subject": subject,
                "body": {
                    "contentType": "HTML",
                    "content": html_body
                },
                "toRecipients": [{"emailAddress": {"address": to_email}}]
            },
            "saveToSentItems": "true"
        }

        try:
            response = requests.post(url, json=email_msg, headers=headers, timeout=10)
            if response.status_code == 202:
                logger.info(f"Email sent via Azure sendMail to {to_email}")
                return {"sent": True, "method": "azure_graph_send"}
            else:
                logger.error(f"Graph API Error {response.status_code}: {response.text}")
                return {"sent": False, "error": response.text}
        except Exception as e:
            logger.error(f"Graph API Exception: {e}")
            return {"sent": False, "error": str(e)}

    def _send_via_smtp(self, to_email, subject, html_body, in_reply_to, references):
        # ... (Logika SMTP tetap sama) ...
        try:
            msg = MIMEMultipart()
            msg['From'] = settings.SMTP_USERNAME or settings.EMAIL_USER
            msg['To'] = to_email
            msg['Subject'] = subject
            msg['Message-ID'] = make_msgid()
            if in_reply_to: msg['In-Reply-To'] = in_reply_to
            if references: msg['References'] = references
            msg.attach(MIMEText(html_body, 'html'))
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
                server.starttls()
                server.login(settings.EMAIL_USER, settings.EMAIL_PASS)
                server.send_message(msg)
            return {"sent": True, "message_id": msg['Message-ID']}
        except Exception as e:
            logger.error(f"SMTP Error: {e}")
            return {"sent": False, "error": str(e)}