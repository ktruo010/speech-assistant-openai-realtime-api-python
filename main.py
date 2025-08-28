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
        "Nếu kết quả không có thông tin cụ thể (như tỷ số trận đấu), hãy nói rõ những gì bạn tìm thấy "
        "và gợi ý người dùng hỏi cách khác hoặc tìm kiếm cụ thể hơn. "
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

def get_place_info(place_name: str, location: str = "") -> str:
    """Get information about a place using Google Places API."""
    if not GOOGLE_API_KEY:
        if LANGUAGE == 'vi':
            return "Google Places API chưa được cấu hình. Vui lòng thiết lập Google API key."
        return "Google Places API is not configured. Please set up Google API key."
    
    try:
        # Build the Google Places service
        service = build("places", "v1", developerKey=GOOGLE_API_KEY)
        
        # Construct search query
        search_query = f"{place_name} {location}".strip()
        
        print("\n" + "="*60)
        print(f"----------- Searching for place: {search_query} --------------")
        print("="*60)
        
        # Search for the place using Text Search
        search_request = {
            "textQuery": search_query,
            "languageCode": "vi" if LANGUAGE == 'vi' else "en"
        }
        
        # Execute the search with field mask
        places_result = service.places().searchText(
            body=search_request,
            fields="places.displayName,places.formattedAddress,places.internationalPhoneNumber,places.nationalPhoneNumber,places.websiteUri,places.regularOpeningHours,places.rating,places.userRatingCount,places.businessStatus,places.id"
        ).execute()
        
        if not places_result.get('places'):
            if LANGUAGE == 'vi':
                return f"Không tìm thấy thông tin về '{place_name}'."
            return f"No information found for '{place_name}'."
        
        # Get the first (most relevant) result
        place = places_result['places'][0]
        
        # Extract place details
        place_id = place.get('id', '')
        
        # Get detailed information about the place
        if place_id:
            # Fields to retrieve
            fields = [
                "displayName",
                "formattedAddress", 
                "nationalPhoneNumber",
                "internationalPhoneNumber",
                "websiteUri",
                "regularOpeningHours",
                "rating",
                "userRatingCount",
                "priceLevel",
                "businessStatus"
            ]
            
            detail_request = service.places().get(
                name=f"places/{place_id}",
                fields=",".join(fields)
            )
            place_details = detail_request.execute()
        else:
            place_details = place
        
        # Format the response
        name = place_details.get('displayName', {}).get('text', place_name)
        address = place_details.get('formattedAddress', 'Không có địa chỉ' if LANGUAGE == 'vi' else 'Address not available')
        phone = place_details.get('nationalPhoneNumber') or place_details.get('internationalPhoneNumber') or ('Không có số điện thoại' if LANGUAGE == 'vi' else 'Phone not available')
        website = place_details.get('websiteUri', '')
        rating = place_details.get('rating', 0)
        review_count = place_details.get('userRatingCount', 0)
        
        # Get opening hours if available
        hours_text = ""
        if 'regularOpeningHours' in place_details:
            opening_hours = place_details['regularOpeningHours']
            if 'weekdayDescriptions' in opening_hours:
                hours_list = opening_hours['weekdayDescriptions']
                if LANGUAGE == 'vi':
                    hours_text = "Giờ mở cửa: " + ", ".join(hours_list[:2]) + "..."
                else:
                    hours_text = "Hours: " + ", ".join(hours_list[:2]) + "..."
        
        # Format response based on language
        if LANGUAGE == 'vi':
            result = f"Thông tin về {name}: "
            result += f"Địa chỉ: {address}. "
            result += f"Điện thoại: {phone}. "
            if website:
                result += f"Website: {website}. "
            if rating > 0:
                result += f"Đánh giá: {rating}/5 sao ({review_count} lượt đánh giá). "
            if hours_text:
                result += hours_text
        else:
            result = f"Information for {name}: "
            result += f"Address: {address}. "
            result += f"Phone: {phone}. "
            if website:
                result += f"Website: {website}. "
            if rating > 0:
                result += f"Rating: {rating}/5 stars ({review_count} reviews). "
            if hours_text:
                result += hours_text
        
        print(f"Found place: {name}")
        print(f"Address: {address}")
        print("="*60 + "\n")
        
        return result
        
    except Exception as e:
        logger.error(f"Error getting place info: {str(e)}")
        if "API key not valid" in str(e):
            if LANGUAGE == 'vi':
                return "Lỗi: Google Places API chưa được kích hoạt. Vui lòng kích hoạt Places API trong Google Cloud Console."
            return "Error: Google Places API is not enabled. Please enable Places API in Google Cloud Console."
        elif "PERMISSION_DENIED" in str(e):
            if LANGUAGE == 'vi':
                return "Lỗi: Không có quyền truy cập Places API. Vui lòng kiểm tra cấu hình API key."
            return "Error: Permission denied for Places API. Please check API key configuration."
        else:
            if LANGUAGE == 'vi':
                return f"Lỗi khi tìm kiếm thông tin: {str(e)}"
            return f"Error searching for place information: {str(e)}"

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

def get_stock_info(symbol: str) -> str:
    """Get current stock information using Yahoo Finance."""
    try:
        import yfinance as yf
        
        logger.info(f"Getting stock info for: {symbol}")
        print(f"\n{'='*60}")
        print(f"----------- Getting stock info: {symbol} --------------")
        print("="*60)
        
        # Get stock data
        stock = yf.Ticker(symbol.upper())
        info = stock.info
        
        # Get current price and basic info
        current_price = info.get('currentPrice', info.get('regularMarketPrice', 0))
        previous_close = info.get('previousClose', info.get('regularMarketPreviousClose', 0))
        
        if current_price == 0:
            # Try to get latest price from history
            hist = stock.history(period="1d")
            if not hist.empty:
                current_price = hist['Close'].iloc[-1]
                
        if current_price == 0:
            if LANGUAGE == 'vi':
                return f"Không tìm thấy thông tin cho mã chứng khoán {symbol.upper()}"
            return f"No information found for stock symbol {symbol.upper()}"
        
        # Calculate change
        change = current_price - previous_close if previous_close else 0
        change_percent = (change / previous_close * 100) if previous_close else 0
        
        # Get additional info
        market_cap = info.get('marketCap', 0)
        volume = info.get('volume', 0)
        day_high = info.get('dayHigh', 0)
        day_low = info.get('dayLow', 0)
        
        # Get timestamp of last trade
        from datetime import datetime
        current_time = datetime.now(pytz.timezone('America/New_York'))
        time_str = current_time.strftime('%H:%M %Z')
        
        # Format response
        if LANGUAGE == 'vi':
            result = f"{info.get('longName', symbol.upper())}: "
            result += f"Giá hiện tại ${current_price:.2f}, "
            result += f"thay đổi {'+' if change >= 0 else ''}{change:.2f} ({'+' if change_percent >= 0 else ''}{change_percent:.2f}%). "
            if day_high and day_low:
                result += f"Trong ngày: thấp ${day_low:.2f}, cao ${day_high:.2f}. "
            if volume:
                result += f"Khối lượng: {volume:,}. "
            result += f"Cập nhật lúc {time_str}"
        else:
            result = f"{info.get('longName', symbol.upper())}: "
            result += f"Current price ${current_price:.2f}, "
            result += f"change {'+' if change >= 0 else ''}{change:.2f} ({'+' if change_percent >= 0 else ''}{change_percent:.2f}%). "
            if day_high and day_low:
                result += f"Day range: ${day_low:.2f} - ${day_high:.2f}. "
            if volume:
                result += f"Volume: {volume:,}. "
            result += f"Last updated {time_str}"
        
        print(f"Stock price: ${current_price:.2f}")
        print("="*60 + "\n")
        
        return result
        
    except Exception as e:
        logger.error(f"Error getting stock info: {str(e)}")
        if LANGUAGE == 'vi':
            return f"Không thể lấy thông tin chứng khoán cho {symbol}. Vui lòng kiểm tra mã chứng khoán."
        return f"Unable to get stock information for {symbol}. Please check the symbol."

def get_directions(origin: str, destination: str, mode: str = "driving") -> str:
    """Get directions between two locations using Google Directions API."""
    try:
        if not GOOGLE_API_KEY:
            if LANGUAGE == 'vi':
                return "Google Directions API chưa được cấu hình."
            return "Google Directions API is not configured."
        
        logger.info(f"Getting directions from {origin} to {destination}")
        print(f"\n{'='*60}")
        print(f"----------- Directions: {origin} → {destination} --------------")
        print("="*60)
        
        service = build("maps", "v1", developerKey=GOOGLE_API_KEY)
        
        # Make request to Directions API
        import requests
        url = "https://maps.googleapis.com/maps/api/directions/json"
        params = {
            "origin": origin,
            "destination": destination,
            "mode": mode,
            "key": GOOGLE_API_KEY,
            "language": "vi" if LANGUAGE == 'vi' else "en"
        }
        
        response = requests.get(url, params=params)
        data = response.json()
        
        if data.get("status") != "OK":
            if LANGUAGE == 'vi':
                return f"Không thể tìm đường từ {origin} đến {destination}"
            return f"Could not find directions from {origin} to {destination}"
        
        # Parse the route
        route = data["routes"][0]
        leg = route["legs"][0]
        
        distance = leg["distance"]["text"]
        duration = leg["duration"]["text"]
        start_address = leg["start_address"]
        end_address = leg["end_address"]
        
        # Get first few steps
        steps_text = []
        for i, step in enumerate(leg["steps"][:3]):
            instruction = step["html_instructions"]
            # Remove HTML tags
            import re
            instruction = re.sub('<.*?>', '', instruction)
            step_distance = step["distance"]["text"]
            steps_text.append(f"{i+1}. {instruction} ({step_distance})")
        
        # Format response
        if LANGUAGE == 'vi':
            result = f"Chỉ đường từ {start_address} đến {end_address}: "
            result += f"Khoảng cách {distance}, thời gian ước tính {duration}. "
            result += "Các bước: " + "; ".join(steps_text)
            if len(leg["steps"]) > 3:
                result += f" và {len(leg['steps'])-3} bước nữa."
        else:
            result = f"Directions from {start_address} to {end_address}: "
            result += f"Distance {distance}, estimated time {duration}. "
            result += "Steps: " + "; ".join(steps_text)
            if len(leg["steps"]) > 3:
                result += f" and {len(leg['steps'])-3} more steps."
        
        print(f"Distance: {distance}, Duration: {duration}")
        print("="*60 + "\n")
        
        return result
        
    except Exception as e:
        logger.error(f"Error getting directions: {str(e)}")
        if LANGUAGE == 'vi':
            return f"Lỗi khi tìm đường: {str(e)}"
        return f"Error getting directions: {str(e)}"

def search_youtube(query: str, max_results: int = 3) -> str:
    """Search YouTube videos using YouTube Data API v3."""
    try:
        if not GOOGLE_API_KEY:
            if LANGUAGE == 'vi':
                return "YouTube API chưa được cấu hình."
            return "YouTube API is not configured."
        
        logger.info(f"Searching YouTube for: {query}")
        print(f"\n{'='*60}")
        print(f"----------- YouTube search: {query} --------------")
        print("="*60)
        
        service = build("youtube", "v3", developerKey=GOOGLE_API_KEY)
        
        # Search for videos
        search_response = service.search().list(
            q=query,
            part="snippet",
            maxResults=max_results,
            type="video",
            relevanceLanguage="vi" if LANGUAGE == 'vi' else "en"
        ).execute()
        
        if not search_response.get("items"):
            if LANGUAGE == 'vi':
                return f"Không tìm thấy video nào cho '{query}'"
            return f"No videos found for '{query}'"
        
        # Format results
        results = []
        for item in search_response["items"]:
            title = item["snippet"]["title"]
            channel = item["snippet"]["channelTitle"]
            video_id = item["id"]["videoId"]
            url = f"youtube.com/watch?v={video_id}"
            results.append(f"{title} bởi {channel} ({url})" if LANGUAGE == 'vi' else f"{title} by {channel} ({url})")
        
        # Format response
        if LANGUAGE == 'vi':
            result = f"Tìm thấy {len(results)} video cho '{query}': "
            result += "; ".join(results)
        else:
            result = f"Found {len(results)} videos for '{query}': "
            result += "; ".join(results)
        
        print(f"Found {len(results)} videos")
        print("="*60 + "\n")
        
        return result
        
    except Exception as e:
        logger.error(f"Error searching YouTube: {str(e)}")
        if LANGUAGE == 'vi':
            return f"Lỗi khi tìm kiếm YouTube: {str(e)}"
        return f"Error searching YouTube: {str(e)}"

def knowledge_graph_search(query: str) -> str:
    """Search Google Knowledge Graph for entity information."""
    try:
        if not GOOGLE_API_KEY:
            if LANGUAGE == 'vi':
                return "Knowledge Graph API chưa được cấu hình."
            return "Knowledge Graph API is not configured."
        
        logger.info(f"Knowledge Graph search for: {query}")
        print(f"\n{'='*60}")
        print(f"----------- Knowledge Graph: {query} --------------")
        print("="*60)
        
        service = build("kgsearch", "v1", developerKey=GOOGLE_API_KEY)
        
        # Search Knowledge Graph
        response = service.entities().search(
            query=query,
            limit=1,
            languages=["vi"] if LANGUAGE == 'vi' else ["en"]
        ).execute()
        
        if not response.get("itemListElement"):
            if LANGUAGE == 'vi':
                return f"Không tìm thấy thông tin về '{query}'"
            return f"No information found for '{query}'"
        
        # Get first result
        entity = response["itemListElement"][0]["result"]
        
        name = entity.get("name", query)
        description = entity.get("description", "")
        detailed_desc = entity.get("detailedDescription", {})
        article_body = detailed_desc.get("articleBody", "")
        
        # Format response
        if LANGUAGE == 'vi':
            result = f"{name}"
            if description:
                result += f" ({description})"
            if article_body:
                result += f": {article_body[:200]}..."
        else:
            result = f"{name}"
            if description:
                result += f" ({description})"
            if article_body:
                result += f": {article_body[:200]}..."
        
        print(f"Found: {name}")
        print("="*60 + "\n")
        
        return result
        
    except Exception as e:
        logger.error(f"Error in Knowledge Graph search: {str(e)}")
        if LANGUAGE == 'vi':
            return f"Lỗi khi tìm kiếm Knowledge Graph: {str(e)}"
        return f"Error in Knowledge Graph search: {str(e)}"

def search_news(query: str, max_results: int = 3) -> str:
    """Search for news using Google Custom Search configured for news."""
    try:
        if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
            if LANGUAGE == 'vi':
                return "Google News Search chưa được cấu hình."
            return "Google News Search is not configured."
        
        logger.info(f"Searching news for: {query}")
        print(f"\n{'='*60}")
        print(f"----------- News search: {query} --------------")
        print("="*60)
        
        # Use Custom Search API with news sites
        service = build("customsearch", "v1", developerKey=GOOGLE_API_KEY)
        
        # Add news-specific parameters
        search_query = f"{query} site:cnn.com OR site:bbc.com OR site:reuters.com OR site:nytimes.com OR site:vnexpress.net OR site:tuoitre.vn"
        
        result = service.cse().list(
            q=search_query,
            cx=GOOGLE_CSE_ID,
            num=max_results,
            dateRestrict="d7"  # Last 7 days
        ).execute()
        
        if 'items' not in result:
            if LANGUAGE == 'vi':
                return f"Không tìm thấy tin tức nào về '{query}'"
            return f"No news found for '{query}'"
        
        # Format results
        news_items = []
        for item in result['items']:
            title = item.get('title', 'No title')
            snippet = item.get('snippet', '')
            news_items.append(f"{title}: {snippet[:100]}...")
        
        # Format response
        if LANGUAGE == 'vi':
            result_text = f"Tin tức mới nhất về '{query}': "
            result_text += "; ".join(news_items)
        else:
            result_text = f"Latest news about '{query}': "
            result_text += "; ".join(news_items)
        
        print(f"Found {len(news_items)} news items")
        print("="*60 + "\n")
        
        return result_text
        
    except Exception as e:
        logger.error(f"Error searching news: {str(e)}")
        if LANGUAGE == 'vi':
            return f"Lỗi khi tìm kiếm tin tức: {str(e)}"
        return f"Error searching news: {str(e)}"

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
        "name": "get_place_info",
        "description": "Get detailed information about a specific business, restaurant, store, or place including address, phone, hours, and reviews. Use this for location/business queries.",
        "parameters": {
            "type": "object",
            "properties": {
                "place_name": {
                    "type": "string",
                    "description": "The name of the place or business (e.g., 'Texas Treasure Casino', 'McDonald's in Times Square', 'Eiffel Tower')"
                },
                "location": {
                    "type": "string",
                    "description": "Optional: City or area to search in (e.g., 'Austin, Texas', 'New York', 'Paris')",
                    "default": ""
                }
            },
            "required": ["place_name"]
        }
    },
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
        "name": "get_stock_info",
        "description": "Get current stock price and information for any stock symbol. Use this for stock market queries.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "The stock symbol (e.g., 'AAPL' for Apple, 'GOOGL' for Google, 'TSLA' for Tesla)"
                }
            },
            "required": ["symbol"]
        }
    },
    {
        "type": "function",
        "name": "get_directions",
        "description": "Get driving or transit directions between two locations.",
        "parameters": {
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "Starting location (address or place name)"
                },
                "destination": {
                    "type": "string",
                    "description": "Destination (address or place name)"
                },
                "mode": {
                    "type": "string",
                    "description": "Travel mode: driving, walking, bicycling, or transit",
                    "default": "driving"
                }
            },
            "required": ["origin", "destination"]
        }
    },
    {
        "type": "function",
        "name": "search_youtube",
        "description": "Search for YouTube videos on any topic.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for on YouTube"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of videos to return (default: 3)",
                    "default": 3
                }
            },
            "required": ["query"]
        }
    },
    {
        "type": "function",
        "name": "knowledge_graph_search",
        "description": "Get information about entities (people, places, organizations, etc.) from Google's Knowledge Graph.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The entity to search for (e.g., 'Barack Obama', 'Eiffel Tower', 'Apple Inc.')"
                }
            },
            "required": ["query"]
        }
    },
    {
        "type": "function",
        "name": "search_news",
        "description": "Search for recent news articles about any topic.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The news topic to search for"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of news articles to return (default: 3)",
                    "default": 3
                }
            },
            "required": ["query"]
        }
    },
    {
        "type": "function",
        "name": "web_search",
        "description": "Search the web for current information about any topic (use specialized functions for stocks, directions, YouTube, news, or business info when appropriate)",
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
                # Clean up snippet for better readability
                snippet = snippet.replace('\xa0', ' ').replace('...', '. ')
                formatted_results += f"Kết quả {i}: {title}. {snippet[:300]}. "
        else:
            formatted_results = f"I found {len(items)} results for '{query}'. "
            for i, item in enumerate(items[:max_results], 1):
                title = item.get('title', '')
                snippet = item.get('snippet', '')
                # Clean up snippet for better readability
                snippet = snippet.replace('\xa0', ' ').replace('...', '. ')
                formatted_results += f"Result {i}: {title}. {snippet[:300]}. "
        
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
                            if name == 'get_place_info':
                                place_name = args.get('place_name', '')
                                location = args.get('location', '')
                                
                                result = get_place_info(place_name, location)
                                
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
                                logger.info(f"Sent place info to OpenAI: {result[:100]}...")
                                
                                # Trigger a response generation
                                await openai_ws.send(json.dumps({"type": "response.create"}))
                                logger.info(f"Function {name} completed and voice response triggered")
                                
                            elif name == 'get_current_time':
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
                                
                            elif name == 'get_stock_info':
                                symbol = args.get('symbol', '')
                                result = get_stock_info(symbol)
                                
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
                                logger.info(f"Sent stock info to OpenAI: {result[:100]}...")
                                
                                # Trigger a response generation
                                await openai_ws.send(json.dumps({"type": "response.create"}))
                                logger.info(f"Function {name} completed and voice response triggered")
                                
                            elif name == 'get_directions':
                                origin = args.get('origin', '')
                                destination = args.get('destination', '')
                                mode = args.get('mode', 'driving')
                                result = get_directions(origin, destination, mode)
                                
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
                                logger.info(f"Sent directions to OpenAI: {result[:100]}...")
                                
                                # Trigger a response generation
                                await openai_ws.send(json.dumps({"type": "response.create"}))
                                logger.info(f"Function {name} completed and voice response triggered")
                                
                            elif name == 'search_youtube':
                                query = args.get('query', '')
                                max_results = args.get('max_results', 3)
                                result = search_youtube(query, max_results)
                                
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
                                logger.info(f"Sent YouTube results to OpenAI: {result[:100]}...")
                                
                                # Trigger a response generation
                                await openai_ws.send(json.dumps({"type": "response.create"}))
                                logger.info(f"Function {name} completed and voice response triggered")
                                
                            elif name == 'knowledge_graph_search':
                                query = args.get('query', '')
                                result = knowledge_graph_search(query)
                                
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
                                logger.info(f"Sent Knowledge Graph result to OpenAI: {result[:100]}...")
                                
                                # Trigger a response generation
                                await openai_ws.send(json.dumps({"type": "response.create"}))
                                logger.info(f"Function {name} completed and voice response triggered")
                                
                            elif name == 'search_news':
                                query = args.get('query', '')
                                max_results = args.get('max_results', 3)
                                result = search_news(query, max_results)
                                
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
                                logger.info(f"Sent news results to OpenAI: {result[:100]}...")
                                
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
