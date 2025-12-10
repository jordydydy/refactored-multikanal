from fastapi import APIRouter, Depends, BackgroundTasks, Request, Query, Response, HTTPException
from app.core.config import settings
from app.schemas.models import IncomingMessage
from app.api.dependencies import get_orchestrator
from app.services.orchestrator import MessageOrchestrator
from app.services.parsers import parse_whatsapp_payload, parse_instagram_payload
import logging

logger = logging.getLogger("api.routes")
router = APIRouter()

@router.get("/whatsapp/webhook")
def verify_whatsapp(
    mode: str = Query(..., alias="hub.mode"),
    token: str = Query(..., alias="hub.verify_token"),
    challenge: str = Query(..., alias="hub.challenge"),
):
    if mode == "subscribe" and token == settings.WHATSAPP_VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")

@router.get("/instagram/webhook")
def verify_instagram(
    mode: str = Query(..., alias="hub.mode"),
    token: str = Query(..., alias="hub.verify_token"),
    challenge: str = Query(..., alias="hub.challenge"),
):
    if mode == "subscribe" and token == settings.INSTAGRAM_VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")

@router.post("/whatsapp/webhook")
async def whatsapp_webhook(
    request: Request,
    bg_tasks: BackgroundTasks,
    orchestrator: MessageOrchestrator = Depends(get_orchestrator)
):
    data = await request.json()
    msg = parse_whatsapp_payload(data)
    
    if msg:
        if msg.metadata and msg.metadata.get("is_feedback"):
            logger.info(f"Feedback Event Received (WA): {msg.metadata['payload']}")
            bg_tasks.add_task(orchestrator.handle_feedback, msg)
        else:
            bg_tasks.add_task(orchestrator.process_message, msg)
            
    return {"status": "ok"}

@router.post("/instagram/webhook")
async def instagram_webhook(
    request: Request,
    bg_tasks: BackgroundTasks,
    orchestrator: MessageOrchestrator = Depends(get_orchestrator)
):
    data = await request.json()
    msg = parse_instagram_payload(data)
    
    if msg:
        if msg.metadata and msg.metadata.get("is_feedback"):
            logger.info(f"Feedback Event Received (IG): {msg.metadata['payload']}")
            bg_tasks.add_task(orchestrator.handle_feedback, msg)
        else:
            bg_tasks.add_task(orchestrator.process_message, msg)
            
    return {"status": "ok"}

@router.post("/api/send/reply")
async def ignore_backend_callback(request: Request):
    return {"status": "ignored"}

@router.post("/api/messages/process")
async def process_message_internal(
    msg: IncomingMessage,
    bg_tasks: BackgroundTasks,
    orchestrator: MessageOrchestrator = Depends(get_orchestrator)
):
    bg_tasks.add_task(orchestrator.process_message, msg)
    return {"status": "queued"}