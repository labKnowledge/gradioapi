from fastapi import FastAPI, WebSocket, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse
import aiohttp
import asyncio
import json
import uuid
import logging
from typing import Optional, Dict, Any
from pydantic import BaseModel, HttpUrl
from aiohttp import ClientTimeout

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ChatConfig:
    def __init__(
        self,
        base_url: str = "https://akhaliq-anychat.hf.space/gradio_api",
        timeout: int = 30,
        max_retries: int = 3,
        function_indices: Optional[Dict[str, int]] = None
    ):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.max_retries = max_retries
        self.function_indices = function_indices or {
            'submit_message': 133,
            'queue_join': 135
        }

class ChatMessage(BaseModel):
    message: str
    stream: bool = True  # Added stream option with default True

class ConfigUpdate(BaseModel):
    base_url: HttpUrl
    timeout: Optional[int] = 30
    max_retries: Optional[int] = 3
    function_indices: Optional[Dict[str, int]] = None

class ChatService:
    def __init__(self, config: Optional[ChatConfig] = None):
        self.config = config or ChatConfig()
        self._client_sessions: Dict[str, aiohttp.ClientSession] = {}
        
    def update_config(self, config_update: ConfigUpdate):
        self.config = ChatConfig(
            base_url=str(config_update.base_url),
            timeout=config_update.timeout,
            max_retries=config_update.max_retries,
            function_indices=config_update.function_indices
        )
        asyncio.create_task(self._cleanup_all_sessions())
        return {
            "status": "success",
            "config": {
                "base_url": self.config.base_url,
                "timeout": self.config.timeout,
                "max_retries": self.config.max_retries,
                "function_indices": self.config.function_indices
            }
        }

    async def _cleanup_all_sessions(self):
        for session_id in list(self._client_sessions.keys()):
            await self.cleanup_session(session_id)
        
    async def _get_session(self, session_id: str) -> aiohttp.ClientSession:
        if session_id not in self._client_sessions or self._client_sessions[session_id].closed:
            timeout = ClientTimeout(total=self.config.timeout)
            self._client_sessions[session_id] = aiohttp.ClientSession(timeout=timeout)
        return self._client_sessions[session_id]

    async def _make_request(
        self,
        session_id: str,
        endpoint: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        url = f"{self.config.base_url}/{endpoint}"
        data["session_hash"] = session_id
        
        for attempt in range(self.config.max_retries):
            try:
                session = await self._get_session(session_id)
                async with session.post(url, json=data) as response:
                    if response.status != 200:
                        raise HTTPException(
                            status_code=response.status,
                            detail="Chat service request failed"
                        )
                    return await response.json()
                    
            except Exception as e:
                if attempt == self.config.max_retries - 1:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Chat service error: {str(e)}"
                    )
                await asyncio.sleep(2 ** attempt)

    async def setup_chat(self, session_id: str, message: str):
        await self._make_request(session_id, "run/predict", {
            "data": [{"text": message, "files": []}, None],
            "event_data": None,
            "fn_index": self.config.function_indices["submit_message"]
        })
        
        await self._make_request(session_id, "queue/join", {
            "data": [None, [[message, None]]],
            "fn_index": self.config.function_indices["queue_join"]
        })
        
        return f"{self.config.base_url}/queue/data?session_hash={session_id}"

    async def stream_response(self, session_id: str, message: str):
        try:
            event_source_url = await self.setup_chat(session_id, message)
            session = await self._get_session(session_id)
            
            async with session.get(event_source_url) as response:
                async for line in response.content:
                    yield line
                    
        except Exception as e:
            error_msg = f"data: {json.dumps({'msg': 'error', 'error': str(e)})}\n\n"
            yield error_msg.encode()

    # async def get_complete_response(self, session_id: str, message: str):
    #     """Get complete response without streaming"""
    #     try:
    #         event_source_url = await self.setup_chat(session_id, message)
    #         session = await self._get_session(session_id)
            
    #         async with session.get(event_source_url) as response:
    #             async for line in response.content:
    #                 line = line.decode('utf-8').strip()
    #                 if not line or not line.startswith('data: '):
    #                     continue
                        
    #                 try:
    #                     data = json.loads(line[6:])
    #                     if data["msg"] == "process_completed":
    #                         return {
    #                             "content": ' '.join([item for sublist in data["output"]["data"] for subsublist in sublist for item in subsublist])
    #                         }
    #                 except json.JSONDecodeError as e:
    #                     return {"error": f"Failed to parse response: {str(e)}"}
                        
    #     except Exception as e:
    #         return {"error": str(e)}


    async def get_complete_response(self, session_id: str, message: str):
        """Get complete response without streaming"""
        try:
            event_source_url = await self.setup_chat(session_id, message)
            session = await self._get_session(session_id)
            
            async with session.get(event_source_url) as response:
                async for line in response.content:
                    line = line.decode('utf-8').strip()
                    if not line or not line.startswith('data: '):
                        continue
                        
                    try:
                        data = json.loads(line[6:])
                        if data["msg"] == "process_completed":
                            # Take only the last sublist which should contain the response
                            # Assuming the response is always in the last position
                            response_data = data["output"]["data"][-1][-1]
                            return {
                                "content": response_data[0] if response_data else ""
                            }
                    except json.JSONDecodeError as e:
                        return {"error": f"Failed to parse response: {str(e)}"}
                        
        except Exception as e:
            return {"error": str(e)}
        

    async def cleanup_session(self, session_id: str):
        if session_id in self._client_sessions:
            session = self._client_sessions[session_id]
            if not session.closed:
                await session.close()
            del self._client_sessions[session_id]

    async def get_current_config(self):
        return {
            "base_url": self.config.base_url,
            "timeout": self.config.timeout,
            "max_retries": self.config.max_retries,
            "function_indices": self.config.function_indices
        }

app = FastAPI(title="Chat API")
chat_service = ChatService()

@app.post("/chat/message")
async def send_message(message: ChatMessage, background_tasks: BackgroundTasks):
    """Send a message and get response (streaming or complete)"""
    session_id = str(uuid.uuid4())[:13]
    background_tasks.add_task(chat_service.cleanup_session, session_id)
    
    if message.stream:
        return StreamingResponse(
            chat_service.stream_response(session_id, message.message),
            media_type="text/event-stream"
        )
    else:
        return await chat_service.get_complete_response(session_id, message.message)

@app.websocket("/chat/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    session_id = str(uuid.uuid4())[:13]
    
    try:
        while True:
            # Receive message as JSON to get stream option
            raw_message = await websocket.receive_text()
            message_data = json.loads(raw_message)
            message = ChatMessage(**message_data)
            
            if message.stream:
                event_source_url = await chat_service.setup_chat(session_id, message.message)
                session = await chat_service._get_session(session_id)
                async with session.get(event_source_url) as response:
                    async for line in response.content:
                        await websocket.send_bytes(line)
            else:
                response = await chat_service.get_complete_response(session_id, message.message)
                await websocket.send_json(response)
                    
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        await websocket.close()
    finally:
        await chat_service.cleanup_session(session_id)

@app.post("/config")
async def update_config(config_update: ConfigUpdate):
    return chat_service.update_config(config_update)

@app.get("/config")
async def get_config():
    return await chat_service.get_current_config()

