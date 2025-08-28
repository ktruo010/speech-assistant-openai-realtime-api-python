import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream
from dotenv import load_dotenv
from googleapiclient.discovery import build
import logging
import pytz
from datetime import datetime

load_dotenv()

# Set up timezone-aware logging
class TimezoneFormatter(logging.Formatter):
    def __init__(self, *args, tz=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tz = tz or pytz.UTC
    
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=pytz.UTC)
        dt = dt.astimezone(self.tz)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime('%Y-%m-%d %H:%M:%S %Z')

# Initial logger setup (will be reconfigured after reading TIMEZONE)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
GOOGLE_CSE_ID = os.getenv('GOOGLE_CSE_ID')
PORT = int(os.getenv('PORT', 5050))
LANGUAGE = os.getenv('LANGUAGE', 'vi')  # Default to Vietnamese
MAX_CALL_DURATION = int(os.getenv('MAX_CALL_DURATION', 3600))  # Default 1 hour (3600 seconds)
PASSCODE = os.getenv('PASSCODE')  # Optional passcode for call authentication
MAX_PASSCODE_ATTEMPTS = int(os.getenv('MAX_PASSCODE_ATTEMPTS', 3))  # Max attempts before hanging up
TIMEZONE = os.getenv('TIMEZONE', 'Asia/Ho_Chi_Minh')  # Default to Vietnam timezone

# Configure timezone
try:
    tz = pytz.timezone(TIMEZONE)
    print(f"⏰ Timezone configured: {TIMEZONE}")
except pytz.exceptions.UnknownTimeZoneError:
    print(f"⚠️ Unknown timezone: {TIMEZONE}. Using UTC instead.")
    TIMEZONE = 'UTC'
    tz = pytz.UTC

# Reconfigure logger with timezone
for handler in logger.handlers[:]:
    logger.removeHandler(handler)
    
handler = logging.StreamHandler()
formatter = TimezoneFormatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    tz=tz
)
handler.setFormatter(formatter)
logger.addHandler(handler)

# System messages for different languages
SYSTEM_MESSAGES = {
    'vi': (
        "Bạn là một trợ lý AI thân thiện và nhiệt tình, sẵn sàng trò chuyện về "
        "bất kỳ chủ đề nào mà người dùng quan tâm và cung cấp thông tin hữu ích. "
        "Bạn có khả năng kể chuyện cười vui vẻ khi phù hợp. "
        "Luôn giữ thái độ tích cực và hỗ trợ người dùng một cách tốt nhất. "
        "Bạn có thể tìm kiếm web để cung cấp thông tin mới nhất khi được yêu cầu. "
        "QUAN TRỌNG: Khi bạn nhận được kết quả từ function web_search hoặc get_current_time, "
        "hãy LUÔN sử dụng thông tin đó để trả lời người dùng. Đọc to và rõ ràng các kết quả tìm kiếm. "
        "Luôn trả lời bằng tiếng Việt."
    ),
    'en': (
        "You are a helpful and bubbly AI assistant who loves to chat about "
        "anything the user is interested in and is prepared to offer them facts. "
        "You have a penchant for dad jokes, owl jokes, and rickrolling – subtly. "
        "Always stay positive, but work in a joke when appropriate. "
        "You have access to web search to find current information when asked. "
        "IMPORTANT: When you receive results from web_search or get_current_time functions, "
        "ALWAYS use that information to answer the user. Read out the search results clearly."
    )
}

SYSTEM_MESSAGE = SYSTEM_MESSAGES.get(LANGUAGE, SYSTEM_MESSAGES['vi'])
VOICE = 'alloy'
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created', 'response.function_call_arguments.done',
    'response.output_item.added', 'conversation.item.created',
    'response.created'
]
SHOW_TIMING_MATH = False

app = FastAPI()

def get_current_time_str():
    """Get current time string in configured timezone."""
    return datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S %Z')

def get_current_time(location: str) -> str:
    """Get the current time for a specific location/timezone."""
    # Common city to timezone mappings
    city_timezones = {
        'new york': 'America/New_York',
        'los angeles': 'America/Los_Angeles',
        'chicago': 'America/Chicago',
        'houston': 'America/Chicago',
        'phoenix': 'America/Phoenix',
        'philadelphia': 'America/New_York',
        'san francisco': 'America/Los_Angeles',
        'london': 'Europe/London',
        'paris': 'Europe/Paris',
        'berlin': 'Europe/Berlin',
        'madrid': 'Europe/Madrid',
        'rome': 'Europe/Rome',
        'moscow': 'Europe/Moscow',
        'tokyo': 'Asia/Tokyo',
        'beijing': 'Asia/Shanghai',
        'shanghai': 'Asia/Shanghai',
        'hong kong': 'Asia/Hong_Kong',
        'singapore': 'Asia/Singapore',
        'delhi': 'Asia/Kolkata',
        'mumbai': 'Asia/Kolkata',
        'bangkok': 'Asia/Bangkok',
        'ho chi minh': 'Asia/Ho_Chi_Minh',
        'hanoi': 'Asia/Ho_Chi_Minh',
        'saigon': 'Asia/Ho_Chi_Minh',
        'sydney': 'Australia/Sydney',
        'melbourne': 'Australia/Melbourne',
        'dubai': 'Asia/Dubai',
        'seoul': 'Asia/Seoul',
        'toronto': 'America/Toronto',
        'vancouver': 'America/Vancouver',
        'mexico city': 'America/Mexico_City',
        'sao paulo': 'America/Sao_Paulo',
        'buenos aires': 'America/Argentina/Buenos_Aires',
    }
    
    try:
        # Check if it's already a timezone format
        if '/' in location:
            timezone_str = location
        else:
            # Try to find the city in our mapping
            city_lower = location.lower().strip()
            timezone_str = city_timezones.get(city_lower)
            
            if not timezone_str:
                # Try partial match
                for city, tz_str in city_timezones.items():
                    if city in city_lower or city_lower in city:
                        timezone_str = tz_str
                        break
            
            if not timezone_str:
                # Default to searching for the location as-is
                # This might fail but we'll catch it
                timezone_str = location
        
        # Get timezone object
        target_tz = pytz.timezone(timezone_str)
        current_time = datetime.now(target_tz)
        
        # Format time based on language
        if LANGUAGE == 'vi':
            # Vietnamese format: HH:MM:SS, ngày DD/MM/YYYY
            time_str = current_time.strftime('%H:%M:%S, ngày %d/%m/%Y')
            timezone_name = current_time.strftime('%Z')
            return f"Bây giờ là {time_str} ({timezone_name}) tại {location}"
        else:
            # English format: HH:MM:SS, Month DD, YYYY
            time_str = current_time.strftime('%I:%M:%S %p, %B %d, %Y')
            timezone_name = current_time.strftime('%Z')
            return f"The current time in {location} is {time_str} ({timezone_name})"
            
    except pytz.exceptions.UnknownTimeZoneError:
        logger.warning(f"Unknown timezone for location: {location}")
        if LANGUAGE == 'vi':
            return f"Xin lỗi, tôi không thể tìm thấy múi giờ cho {location}. Vui lòng thử với tên thành phố lớn như New York, London, Tokyo."
        else:
            return f"Sorry, I couldn't find the timezone for {location}. Please try with a major city name like New York, London, Tokyo."
    except Exception as e:
        logger.error(f"Error getting time for {location}: {str(e)}")
        if LANGUAGE == 'vi':
            return f"Xin lỗi, tôi gặp lỗi khi lấy thời gian cho {location}."
        else:
            return f"Sorry, I encountered an error while getting the time for {location}."

if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
    logger.warning('Google Search API credentials not found. Web search will be disabled.')
    logger.warning('To enable web search, set GOOGLE_API_KEY and GOOGLE_CSE_ID in the .env file.')

if PASSCODE:
    logger.info(f'Passcode protection enabled. Passcode length: {len(PASSCODE)} digits')
    logger.info(f'Maximum passcode attempts: {MAX_PASSCODE_ATTEMPTS}')
    print(f"\n🔒 Passcode protection enabled ({len(PASSCODE)} digits, {MAX_PASSCODE_ATTEMPTS} attempts)")
else:
    logger.info('No passcode protection configured')
    print("\n🔓 No passcode protection")

# Tool definitions for OpenAI
TOOLS = [
    {
        "type": "function",
        "name": "get_current_time",
        "description": "Get the current time in any city or timezone. Use this for time-related queries instead of web search.",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "The city name or timezone (e.g., 'New York', 'London', 'Tokyo', 'America/New_York', 'Europe/London')"
                }
            },
            "required": ["location"]
        }
    },
    {
        "type": "function",
        "name": "web_search",
        "description": "Search the web for current information about any topic (except current time - use get_current_time for that)",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to look up on the web"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of search results to return (default: 3)",
                    "default": 3
                }
            },
            "required": ["query"]
        }
    }
]

def web_search_sync(query: str, max_results: int = 3) -> str:
    """Perform a web search using Google Custom Search API."""
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        if LANGUAGE == 'vi':
            return "Tìm kiếm web chưa được cấu hình. Vui lòng thiết lập Google API."
        return "Web search is not configured. Please set up Google API credentials."
    
    try:
        # Enhanced console logging for web search
        print("\n" + "="*60)
        print(f"----------- Web search: {query} --------------")
        print("="*60)
        logger.info(f"Performing Google search for: {query}")
        service = build("customsearch", "v1", developerKey=GOOGLE_API_KEY)
        
        # Execute the search
        result = service.cse().list(
            q=query,
            cx=GOOGLE_CSE_ID,
            num=min(max_results, 10)  # Google API max is 10 per request
        ).execute()
        
        items = result.get('items', [])
        
        if not items:
            if LANGUAGE == 'vi':
                return "Không tìm thấy kết quả nào."
            return "No search results found."
        
        # Format results for voice response
        if LANGUAGE == 'vi':
            formatted_results = f"Tôi tìm thấy {len(items)} kết quả cho '{query}'. "
            for i, item in enumerate(items[:max_results], 1):
                title = item.get('title', '')
                snippet = item.get('snippet', '')
                formatted_results += f"Kết quả {i}: {title}. {snippet[:200]}... "
        else:
            formatted_results = f"I found {len(items)} results for '{query}'. "
            for i, item in enumerate(items[:max_results], 1):
                title = item.get('title', '')
                snippet = item.get('snippet', '')
                formatted_results += f"Result {i}: {title}. {snippet[:200]}... "
        
        logger.info(f"Search completed with {len(items)} results")
        print(f"Search completed. Found {len(items)} results.")
        print("="*60 + "\n")
        return formatted_results
    except Exception as e:
        logger.error(f"Error during Google search: {str(e)}")
        return f"Sorry, I encountered an error while searching: {str(e)}"

async def web_search(query: str, max_results: int = 3) -> str:
    """Async wrapper for web search."""
    # Run the synchronous function in a thread pool to avoid blocking
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, web_search_sync, query, max_results)

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    response = VoiceResponse()
    host = request.url.hostname
    
    # If passcode is configured, ask for it immediately
    if PASSCODE:
        gather = response.gather(
            num_digits=len(PASSCODE),
            action=f'https://{host}/verify-passcode',
            method='POST',
            timeout=10,
            finish_on_key='#'
        )
        # Always use English for passcode prompt
        gather.say("Please enter your passcode")
        
        # If user doesn't enter anything, hang up
        response.say("No passcode received. Goodbye.")
        response.hangup()
    else:
        # No passcode required, connect directly
        # Skip Twilio greeting and let OpenAI handle the greeting
        connect = Connect()
        connect.stream(url=f'wss://{host}/media-stream')
        response.append(connect)
    
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.api_route("/verify-passcode", methods=["POST"])
async def verify_passcode(request: Request):
    """Verify the passcode entered by the caller."""
    form_data = await request.form()
    digits = form_data.get('Digits', '')
    attempt = int(form_data.get('attempt', '1'))
    
    response = VoiceResponse()
    host = request.url.hostname
    
    # Log passcode attempt
    logger.info(f"Passcode attempt {attempt}: {'*' * len(digits)} (length: {len(digits)})")
    
    if digits == PASSCODE:
        # Passcode correct - connect to AI assistant
        logger.info("Passcode verified successfully")
        print("\n✅ Passcode verified successfully")
        
        # Skip Twilio greeting and connect directly - let OpenAI handle the greeting
        connect = Connect()
        connect.stream(url=f'wss://{host}/media-stream')
        response.append(connect)
    else:
        # Passcode incorrect
        logger.warning(f"Incorrect passcode attempt {attempt}")
        print(f"\n❌ Incorrect passcode attempt {attempt}/{MAX_PASSCODE_ATTEMPTS}")
        
        if attempt < MAX_PASSCODE_ATTEMPTS:
            # Allow another attempt
            gather = response.gather(
                num_digits=len(PASSCODE),
                action=f'https://{host}/verify-passcode?attempt={attempt + 1}',
                method='POST',
                timeout=10,
                finish_on_key='#'
            )
            
            remaining_attempts = MAX_PASSCODE_ATTEMPTS - attempt
            # Always use English for passcode retry prompts
            gather.say(f"Incorrect passcode. You have {remaining_attempts} attempts remaining. Please enter the passcode again.")
            
            # If user doesn't enter anything
            response.say("No passcode received. Goodbye.")  
            response.hangup()
        else:
            # Max attempts reached - hang up
            logger.warning("Max passcode attempts reached. Hanging up.")
            print("\n🚫 Max passcode attempts reached. Hanging up.")
            
            # Always use English for max attempts message
            response.say("Maximum attempts exceeded. Goodbye.")
            response.hangup()
    
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    print(f"\n📞 Client connected at {get_current_time_str()}")
    await websocket.accept()

    async with websockets.connect(
        'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }
    ) as openai_ws:
        await initialize_session(openai_ws)

        # Connection specific state
        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None
        call_start_time = asyncio.get_event_loop().time()
        
        # Log call duration settings
        if MAX_CALL_DURATION:
            duration_mins = MAX_CALL_DURATION / 60
            logger.info(f"Call duration limit set to {duration_mins} minutes ({MAX_CALL_DURATION} seconds)")
            print(f"\n📞 Call duration limit: {duration_mins} minutes")
        else:
            logger.info("No call duration limit set")
            print("\n📞 No call duration limit")
        
        async def check_call_duration():
            """Check if call has exceeded maximum duration and terminate if necessary."""
            if not MAX_CALL_DURATION:
                return False
            
            current_time = asyncio.get_event_loop().time()
            elapsed_time = current_time - call_start_time
            
            if elapsed_time >= MAX_CALL_DURATION:
                logger.info(f"Call duration limit reached ({MAX_CALL_DURATION} seconds)")
                print(f"\n⏰ Call duration limit reached ({MAX_CALL_DURATION} seconds). Ending call...")
                
                # Send a goodbye message before disconnecting
                if stream_sid and websocket.client_state.value == 1:  # Check if connection is open
                    goodbye_message = {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{
                                "type": "input_text",
                                "text": "Xin lỗi, cuộc gọi đã đạt giới hạn thời gian. Cảm ơn bạn đã gọi. Tạm biệt!" if LANGUAGE == 'vi' else "I'm sorry, but we've reached the call time limit. Thank you for calling. Goodbye!"
                            }]
                        }
                    }
                    if openai_ws.open:
                        await openai_ws.send(json.dumps(goodbye_message))
                        await openai_ws.send(json.dumps({"type": "response.create"}))
                        await asyncio.sleep(3)  # Give time for the message to be spoken
                
                return True
            
            # Log remaining time every 5 minutes
            if int(elapsed_time) % 300 == 0 and int(elapsed_time) > 0:
                remaining_time = MAX_CALL_DURATION - elapsed_time
                remaining_mins = remaining_time / 60
                logger.info(f"Call time remaining: {remaining_mins:.1f} minutes")
                
            return False
        
        async def receive_from_twilio():
            """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
            nonlocal stream_sid, latest_media_timestamp
            try:
                async for message in websocket.iter_text():
                    # Check call duration limit
                    if await check_call_duration():
                        break
                    
                    data = json.loads(message)
                    if data['event'] == 'media' and openai_ws.open:
                        latest_media_timestamp = int(data['media']['timestamp'])
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }
                        await openai_ws.send(json.dumps(audio_append))
                    elif data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        print(f"Incoming stream has started {stream_sid}")
                        response_start_timestamp_twilio = None
                        latest_media_timestamp = 0
                        last_assistant_item = None
                    elif data['event'] == 'mark':
                        if mark_queue:
                            mark_queue.pop(0)
            except WebSocketDisconnect:
                print("Client disconnected.")
                if openai_ws.open:
                    await openai_ws.close()

        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
            try:
                async for openai_message in openai_ws:
                    # Check call duration limit
                    if await check_call_duration():
                        break
                    
                    response = json.loads(openai_message)
                    if response['type'] in LOG_EVENT_TYPES:
                        print(f"Received event: {response['type']}", response)

                    if response.get('type') == 'response.audio.delta' and 'delta' in response:
                        audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                        audio_delta = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": audio_payload
                            }
                        }
                        await websocket.send_json(audio_delta)

                        if response_start_timestamp_twilio is None:
                            response_start_timestamp_twilio = latest_media_timestamp
                            if SHOW_TIMING_MATH:
                                print(f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms")

                        # Update last_assistant_item safely
                        if response.get('item_id'):
                            last_assistant_item = response['item_id']

                        await send_mark(websocket, stream_sid)

                    # Handle function calls
                    if response.get('type') == 'response.function_call_arguments.done':
                        logger.info("Function call requested")
                        event_id = response.get('event_id')
                        item_id = response.get('item_id')
                        call_id = response.get('call_id')
                        name = response.get('name')
                        arguments = response.get('arguments')
                        
                        try:
                            args = json.loads(arguments) if arguments else {}
                            logger.info(f"Calling function {name} with arguments: {args}")
                            
                            # Execute the function based on the name
                            if name == 'get_current_time':
                                location = args.get('location', '')
                                # Log time request
                                print("\n" + "="*60)
                                print(f"----------- Getting time for: {location} --------------")
                                print("="*60)
                                
                                result = get_current_time(location)
                                
                                print(f"Time result: {result}")
                                print("="*60 + "\n")
                                
                                # Send function output back to OpenAI
                                function_output = {
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "function_call_output",
                                        "call_id": call_id,
                                        "output": result
                                    }
                                }
                                await openai_ws.send(json.dumps(function_output))
                                logger.info(f"Sent time result to OpenAI: {result}")
                                
                                # Trigger a response generation with explicit instructions
                                await openai_ws.send(json.dumps({"type": "response.create"}))
                                logger.info(f"Function {name} completed and voice response triggered")
                                
                            elif name == 'web_search':
                                query = args.get('query', '')
                                max_results = args.get('max_results', 3)
                                result = await web_search(query, max_results)
                                
                                # Send function output back to OpenAI
                                function_output = {
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "function_call_output",
                                        "call_id": call_id,
                                        "output": result
                                    }
                                }
                                await openai_ws.send(json.dumps(function_output))
                                logger.info(f"Sent web search results to OpenAI (length: {len(result)} chars)")
                                print(f"\n📤 Sent to OpenAI: {result[:200]}...")
                                
                                # Trigger a response generation
                                await openai_ws.send(json.dumps({"type": "response.create"}))
                                logger.info(f"Function {name} completed and voice response triggered")
                            else:
                                logger.warning(f"Unknown function called: {name}")
                        except Exception as e:
                            logger.error(f"Error executing function {name}: {str(e)}")
                            # Send error back to OpenAI
                            error_output = {
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": f"Error executing function: {str(e)}"
                                }
                            }
                            await openai_ws.send(json.dumps(error_output))
                            await openai_ws.send(json.dumps({"type": "response.create"}))

                    # Trigger an interruption. Your use case might work better using `input_audio_buffer.speech_stopped`, or combining the two.
                    if response.get('type') == 'input_audio_buffer.speech_started':
                        print("Speech started detected.")
                        if last_assistant_item:
                            print(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()
            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        async def handle_speech_started_event():
            """Handle interruption when the caller's speech starts."""
            nonlocal response_start_timestamp_twilio, last_assistant_item
            print("Handling speech started event.")
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                if SHOW_TIMING_MATH:
                    print(f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms")

                if last_assistant_item:
                    if SHOW_TIMING_MATH:
                        print(f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms")

                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time
                    }
                    await openai_ws.send(json.dumps(truncate_event))

                await websocket.send_json({
                    "event": "clear",
                    "streamSid": stream_sid
                })

                mark_queue.clear()
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def send_mark(connection, stream_sid):
            if stream_sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": "responsePart"}
                }
                await connection.send_json(mark_event)
                mark_queue.append('responsePart')

        await asyncio.gather(receive_from_twilio(), send_to_twilio())

async def send_initial_conversation_item(openai_ws):
    """Send initial conversation item if AI talks first."""
    # Use appropriate greeting based on language
    if LANGUAGE == 'vi':
        greeting_text = "Chào bạn và nói: Xin chào, tôi có thể giúp gì cho bạn hôm nay?"
    else:
        greeting_text = "Greet the user and say: Hello, how can I help you today?"
    
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": greeting_text
                }
            ]
        }
    }
    await openai_ws.send(json.dumps(initial_conversation_item))
    await openai_ws.send(json.dumps({"type": "response.create"}))


async def initialize_session(openai_ws):
    """Control initial session with OpenAI."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": SYSTEM_MESSAGE,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
            "tools": TOOLS,
            "tool_choice": "auto"
        }
    }
    print('Sending session update:', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))

    # Have the AI speak first with proper greeting
    await send_initial_conversation_item(openai_ws)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
