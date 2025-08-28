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
        "Luôn giữ thái độ tích cực và hỗ trợ người dùng một cách tốt nhất.\n\n"
        "QUY TRÌNH TRẢ LỜI:\n"
        "1. TRƯỚC TIÊN: Sử dụng kiến thức có sẵn của bạn để trả lời nếu thông tin không cần phải mới nhất\n"
        "2. CHỈ sử dụng các function khi:\n"
        "   - Cần thông tin THỜI GIAN THỰC (giá cổ phiếu hiện tại, tin tức mới nhất, thời tiết)\n"
        "   - Cần thông tin ĐỊA ĐIỂM CỤ THỂ (địa chỉ, số điện thoại, giờ mở cửa)\n"
        "   - Cần CHỈ ĐƯỜNG hoặc tìm VIDEO trên YouTube\n"
        "   - Người dùng YÊU CẦU RÕ RÀNG tìm kiếm web\n"
        "3. Khi nhận được kết quả từ function, LUÔN sử dụng thông tin đó để trả lời\n"
        "4. Nếu kết quả không đủ chi tiết, hãy nói rõ và gợi ý cách hỏi cụ thể hơn\n"
        "Luôn trả lời bằng tiếng Việt."
    ),
    'en': (
        "You are a helpful and bubbly AI assistant who loves to chat about "
        "anything the user is interested in and is prepared to offer them facts. "
        "You have a penchant for dad jokes, owl jokes, and rickrolling – subtly. "
        "Always stay positive, but work in a joke when appropriate.\n\n"
        "RESPONSE STRATEGY:\n"
        "1. FIRST: Use your built-in knowledge for general information that doesn't need to be current\n"
        "2. ONLY use functions when:\n"
        "   - Need REAL-TIME info (current stock prices, latest news, weather)\n"
        "   - Need SPECIFIC LOCATION details (address, phone, hours)\n"
        "   - Need DIRECTIONS or YouTube videos\n"
        "   - User EXPLICITLY asks to search the web\n"
        "3. When you receive function results, ALWAYS use that information in your response\n"
        "4. If results lack detail, acknowledge this and suggest more specific queries"
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

def extract_webpage_content(url: str, max_length: int = 5000) -> str:
    """Extract and clean text content from a webpage."""
    import requests
    from bs4 import BeautifulSoup
    import re
    
    try:
        # Set headers to appear as a browser
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        # Fetch the webpage with a timeout
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        
        # Parse HTML
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Remove script and style elements
        for script in soup(["script", "style", "meta", "link", "noscript"]):
            script.decompose()
        
        # Get text content
        text = soup.get_text(separator=' ', strip=True)
        
        # Clean up whitespace
        text = re.sub(r'\s+', ' ', text)
        
        # Limit length if needed
        if len(text) > max_length:
            text = text[:max_length] + "..."
        
        return text
        
    except Exception as e:
        logger.debug(f"Could not extract content from {url}: {str(e)}")
        return ""

def google_search_fallback(query: str, deep_search: bool = True) -> str:
    """Fallback to Google search when specific APIs fail or aren't available.
    Now provides much more comprehensive results with content extraction."""
    try:
        if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
            if LANGUAGE == 'vi':
                return "Không thể thực hiện tìm kiếm. Vui lòng cấu hình Google API."
            return "Unable to perform search. Please configure Google API."
        
        logger.info(f"Using Google search fallback for: {query}")
        print(f"\n⚠️  Falling back to general Google search for: {query}")
        
        service = build("customsearch", "v1", developerKey=GOOGLE_API_KEY)
        result = service.cse().list(
            q=query,
            cx=GOOGLE_CSE_ID,
            num=10  # Fetch more results for comprehensive coverage
        ).execute()
        
        items = result.get('items', [])
        
        if not items:
            if LANGUAGE == 'vi':
                return f"Không tìm thấy kết quả nào cho '{query}'"
            return f"No results found for '{query}'"
        
        # Format results with much more detail
        results_text = []
        sources_used = []
        
        for i, item in enumerate(items[:8], 1):  # Process up to 8 results for thoroughness
            title = item.get('title', '')
            snippet = item.get('snippet', '').replace('\xa0', ' ').replace('...', '. ')
            link = item.get('link', '')
            display_link = item.get('displayLink', '')
            
            # Build comprehensive result text
            result_text = f"Source {i} - {title}"
            if display_link:
                result_text += f" (from {display_link})"
            result_text += f": {snippet}"
            
            # If deep search is enabled and we're in the top 5 results, try to extract more content
            if deep_search and i <= 5 and link:
                extra_content = extract_webpage_content(link, max_length=2000)
                if extra_content and len(extra_content) > len(snippet):
                    # We got more detailed content
                    result_text += f" Additional details: {extra_content[:1500]}"
            
            results_text.append(result_text)
            sources_used.append(display_link)
        
        # Create comprehensive response
        comprehensive_results = []
        comprehensive_results.append(f"Found {len(items)} relevant sources for '{query}'.")
        comprehensive_results.append(f"Here's a comprehensive analysis from {len(results_text)} sources:")
        comprehensive_results.extend(results_text)
        
        # Add source summary
        unique_sources = list(set(sources_used))
        if unique_sources:
            comprehensive_results.append(f"Information gathered from: {', '.join(unique_sources[:5])}")
        
        if LANGUAGE == 'vi':
            return "Kết quả tìm kiếm chi tiết: " + " ".join(comprehensive_results)
        else:
            return " ".join(comprehensive_results)
            
    except Exception as e:
        logger.error(f"Fallback search also failed: {str(e)}")
        if LANGUAGE == 'vi':
            return f"Không thể tìm kiếm thông tin về '{query}'"
        return f"Unable to search for information about '{query}'"

def get_place_info(place_name: str, location: str = "") -> str:
    """Get information about a place using Google Places API."""
    if not GOOGLE_API_KEY:
        # Fallback to general search for place information
        search_query = f"{place_name} {location}".strip() + " address phone hours reviews"
        return google_search_fallback(search_query)
    
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
        logger.error(f"Error getting place info: {str(e)}, falling back to general search")
        # Fallback to general search on any error
        search_query = f"{place_name} {location}".strip() + " address phone hours reviews"
        return google_search_fallback(search_query)

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
    """Get current stock information using Google Finance search with fallback."""
    try:
        if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
            # Fallback to general web search
            return google_search_fallback(f"{symbol} stock price current value")
        
        logger.info(f"Getting stock info for: {symbol}")
        print(f"\n{'='*60}")
        print(f"----------- Getting stock info: {symbol} --------------")
        print("="*60)
        
        # Search for stock information using Google Custom Search
        service = build("customsearch", "v1", developerKey=GOOGLE_API_KEY)
        
        # Search specifically on finance sites for current stock price
        search_query = f"{symbol.upper()} stock price quote site:finance.yahoo.com OR site:google.com/finance OR site:marketwatch.com"
        
        result = service.cse().list(
            q=search_query,
            cx=GOOGLE_CSE_ID,
            num=3
        ).execute()
        
        items = result.get('items', [])
        
        if not items:
            # Fallback to general search if no specific results
            logger.info(f"No specific finance results, falling back to general search for {symbol}")
            return google_search_fallback(f"{symbol} stock price current value")
        
        # Extract information from search results
        stock_info = []
        for item in items[:2]:  # Use first 2 results for better coverage
            title = item.get('title', '')
            snippet = item.get('snippet', '')
            
            # Try to extract price information from snippet
            import re
            # Look for price patterns like $XXX.XX or XXX.XX
            price_match = re.search(r'\$?(\d+\.?\d*)', snippet)
            if price_match:
                price = price_match.group(1)
                
            # Clean up snippet
            snippet = snippet.replace('\xa0', ' ').replace('...', '. ')
            stock_info.append(f"{title}: {snippet[:200]}")
        
        # Get timestamp
        from datetime import datetime
        current_time = datetime.now(pytz.timezone('America/New_York'))
        time_str = current_time.strftime('%H:%M %Z')
        
        # Format response
        if LANGUAGE == 'vi':
            result = f"Thông tin chứng khoán {symbol.upper()}: "
            result += ". ".join(stock_info)
            result += f" Cập nhật lúc {time_str}."
        else:
            result = f"Stock information for {symbol.upper()}: "
            result += ". ".join(stock_info)
            result += f" Last updated {time_str}."
        
        print(f"Found stock info for {symbol.upper()}")
        print("="*60 + "\n")
        
        return result
        
    except Exception as e:
        logger.error(f"Error getting stock info: {str(e)}, falling back to general search")
        # Fallback to general search on any error
        return google_search_fallback(f"{symbol} stock price current value")

def get_directions(origin: str, destination: str, mode: str = "driving") -> str:
    """Get directions between two locations using Google Directions API."""
    try:
        if not GOOGLE_API_KEY:
            # Fallback to general search for directions
            return google_search_fallback(f"driving directions distance time from {origin} to {destination} maps")
        
        logger.info(f"Getting directions from {origin} to {destination}")
        print(f"\n{'='*60}")
        print(f"----------- Directions: {origin} → {destination} --------------")
        print("="*60)
        
        # Directions API doesn't use the build() method - use direct HTTP request
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
        logger.error(f"Error getting directions: {str(e)}, falling back to general search")
        # Fallback to general search on error with improved query
        fallback_query = f"driving directions distance time from {origin} to {destination} maps"
        return google_search_fallback(fallback_query)

def search_youtube(query: str, max_results: int = 3) -> str:
    """Search YouTube videos using YouTube Data API v3."""
    try:
        if not GOOGLE_API_KEY:
            # Fallback to search for YouTube videos
            return google_search_fallback(f"site:youtube.com {query}")
        
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
        logger.error(f"Error searching YouTube: {str(e)}, falling back to general search")
        # Fallback to search YouTube via Google
        return google_search_fallback(f"site:youtube.com {query}")

def knowledge_graph_search(query: str) -> str:
    """Search Google Knowledge Graph for entity information."""
    try:
        if not GOOGLE_API_KEY:
            # Fallback to general search for entity information
            return google_search_fallback(f"{query} wikipedia facts information")
        
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
        logger.error(f"Error in Knowledge Graph search: {str(e)}, falling back to general search")
        # Fallback to general search
        return google_search_fallback(f"{query} wikipedia facts information")

def search_news(query: str, max_results: int = 5) -> str:
    """Search for comprehensive news coverage using Google Custom Search with article content extraction."""
    try:
        if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
            # Use enhanced fallback for news
            return google_search_fallback(f"{query} latest news today breaking updates", deep_search=True)
        
        logger.info(f"Searching comprehensive news for: {query}")
        print(f"\n{'='*60}")
        print(f"----------- Deep News Search: {query} --------------")
        print("="*60)
        
        # Use Custom Search API with major news sites
        service = build("customsearch", "v1", developerKey=GOOGLE_API_KEY)
        
        # Expanded news sources for better coverage
        news_sources = "site:cnn.com OR site:bbc.com OR site:reuters.com OR site:nytimes.com OR site:washingtonpost.com OR site:theguardian.com OR site:apnews.com OR site:bloomberg.com OR site:forbes.com OR site:vnexpress.net OR site:tuoitre.vn"
        search_query = f"{query} {news_sources}"
        
        result = service.cse().list(
            q=search_query,
            cx=GOOGLE_CSE_ID,
            num=10,  # Get more results for comprehensive coverage
            dateRestrict="d7"  # Last 7 days
        ).execute()
        
        items = result.get('items', [])
        
        if not items:
            # Try broader search without site restrictions
            result = service.cse().list(
                q=f"{query} news latest breaking",
                cx=GOOGLE_CSE_ID,
                num=10,
                dateRestrict="d3"  # Last 3 days
            ).execute()
            items = result.get('items', [])
        
        if not items:
            if LANGUAGE == 'vi':
                return f"Không tìm thấy tin tức nào về '{query}'"
            return f"No news found for '{query}'"
        
        # Process news with deep content extraction
        comprehensive_news = []
        news_sources_used = []
        key_facts = []
        
        print(f"Analyzing {len(items)} news sources for comprehensive coverage...")
        
        for i, item in enumerate(items[:max_results], 1):
            title = item.get('title', 'No title')
            snippet = item.get('snippet', '')
            link = item.get('link', '')
            display_link = item.get('displayLink', '')
            
            # Clean up snippet
            snippet = snippet.replace('\xa0', ' ').replace('...', '. ')
            
            # Track sources
            if display_link:
                news_sources_used.append(display_link)
            
            # Build article summary
            article_info = f"Article {i} - {title} [{display_link}]: {snippet}"
            
            # Extract full article content for top stories
            if i <= 3 and link:
                print(f"  Extracting full article from {display_link}...")
                article_content = extract_webpage_content(link, max_length=4000)
                
                if article_content and len(article_content) > len(snippet) * 3:
                    # Got substantial article content
                    article_info += f" FULL ARTICLE EXCERPT: {article_content[:3000]}"
                    
                    # Try to extract key facts (dates, numbers, quotes)
                    import re
                    # Look for quoted statements
                    quotes = re.findall(r'"([^"]{20,150})"', article_content)
                    if quotes:
                        key_facts.extend(quotes[:2])
                    # Look for statistics
                    stats = re.findall(r'\b\d{1,3}(?:,\d{3})*(?:\.\d+)?%?|\$[\d,]+(?:\.\d+)?[BMK]?\b', article_content)
                    if stats:
                        key_facts.extend(stats[:3])
            
            comprehensive_news.append(article_info)
        
        # Build comprehensive news response
        if LANGUAGE == 'vi':
            result_text = f"Phân tích tin tức toàn diện về '{query}' từ {len(items)} nguồn (7 ngày qua): "
            result_text += " ".join(comprehensive_news)
            if key_facts:
                result_text += f" Các điểm chính: {'; '.join(set(key_facts[:5]))}"
            result_text += f" Nguồn tin: {', '.join(set(news_sources_used[:5]))}"
        else:
            result_text = f"Comprehensive news analysis for '{query}' from {len(items)} sources (past 7 days): "
            result_text += " ".join(comprehensive_news)
            if key_facts:
                result_text += f" Key facts and quotes: {'; '.join(set(key_facts[:5]))}"
            result_text += f" News sources: {', '.join(set(news_sources_used[:5]))}"
        
        print(f"Deep news search completed. Analyzed {len(comprehensive_news)} articles in detail")
        print("="*60 + "\n")
        
        return result_text
        
    except Exception as e:
        logger.error(f"Error searching news: {str(e)}, falling back to enhanced general search")
        # Enhanced fallback for news search
        return google_search_fallback(f"{query} latest news today breaking updates", deep_search=True)

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
        "description": "ONLY use for getting CURRENT address, phone, hours of SPECIFIC businesses/places. Not for general info about places.",
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
        "description": "ONLY use for getting the CURRENT time in a specific location. Not for historical times or general time questions.",
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
        "description": "ONLY use for REAL-TIME stock prices. For historical data or general company info, use your knowledge.",
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
        "description": "ONLY use for step-by-step navigation directions. For general distance/travel questions, use your knowledge.",
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
        "description": "ONLY use when user wants to find specific YouTube videos. Not for general video information.",
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
        "description": "ONLY use when your knowledge is insufficient and need current facts about entities. Try your knowledge first.",
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
        "description": "Comprehensive news search with full article extraction. Provides in-depth coverage from multiple sources. Use for current events and news from the LAST 7 DAYS.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The news topic to search for"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of news articles to analyze in detail (default: 5, max: 10)",
                    "default": 5
                }
            },
            "required": ["query"]
        }
    },
    {
        "type": "function",
        "name": "web_search",
        "description": "Comprehensive web search with deep content extraction from multiple sources. Provides thorough analysis of any topic. Use when: 1) User asks to search web, 2) Need current information, 3) Want detailed coverage of a topic.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to look up on the web"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of sources to analyze in detail (default: 5, max: 10)",
                    "default": 5
                }
            },
            "required": ["query"]
        }
    }
]

def web_search_sync(query: str, max_results: int = 5) -> str:
    """Perform a comprehensive web search using Google Custom Search API with deep content extraction."""
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        if LANGUAGE == 'vi':
            return "Tìm kiếm web chưa được cấu hình. Vui lòng thiết lập Google API."
        return "Web search is not configured. Please set up Google API credentials."
    
    try:
        # Enhanced console logging for web search
        print("\n" + "="*60)
        print(f"----------- Deep Web Search: {query} --------------")
        print("="*60)
        logger.info(f"Performing comprehensive Google search for: {query}")
        service = build("customsearch", "v1", developerKey=GOOGLE_API_KEY)
        
        # Execute the search - always get 10 results for comprehensive coverage
        result = service.cse().list(
            q=query,
            cx=GOOGLE_CSE_ID,
            num=10  # Get maximum results
        ).execute()
        
        items = result.get('items', [])
        
        if not items:
            if LANGUAGE == 'vi':
                return "Không tìm thấy kết quả nào."
            return "No search results found."
        
        # Process results with deep content extraction
        comprehensive_results = []
        sources_analyzed = []
        
        print(f"Analyzing {len(items)} sources for comprehensive information...")
        
        # Process all 10 results but extract deep content from top ones
        for i, item in enumerate(items, 1):
            title = item.get('title', '')
            snippet = item.get('snippet', '')
            link = item.get('link', '')
            display_link = item.get('displayLink', '')
            
            # Clean up snippet
            snippet = snippet.replace('\xa0', ' ').replace('...', '. ')
            
            # Build comprehensive source information
            if display_link:
                sources_analyzed.append(display_link)
            
            # For top results, try to extract more content
            if i <= min(max_results, 5) and link:
                print(f"  Extracting detailed content from source {i}: {display_link}...")
                detailed_content = extract_webpage_content(link, max_length=3000)
                
                if detailed_content and len(detailed_content) > len(snippet) * 2:
                    # Got significant additional content
                    result_text = f"Source {i} - {title} [{display_link}]: {snippet} DETAILED INFORMATION: {detailed_content[:2000]}"
                else:
                    # Use enhanced snippet
                    result_text = f"Source {i} - {title} [{display_link}]: {snippet[:500]}"
            else:
                # For remaining results, use snippets
                result_text = f"Source {i} - {title} [{display_link}]: {snippet[:400]}"
            
            comprehensive_results.append(result_text)
        
        # Build the comprehensive response
        if LANGUAGE == 'vi':
            formatted_results = f"Phân tích toàn diện về '{query}' từ {len(items)} nguồn. "
            formatted_results += " ".join(comprehensive_results[:max_results])
            formatted_results += f" Thông tin từ: {', '.join(set(sources_analyzed[:5]))}"
        else:
            formatted_results = f"Comprehensive analysis of '{query}' from {len(items)} sources. "
            formatted_results += " ".join(comprehensive_results[:max_results])
            formatted_results += f" Information gathered from: {', '.join(set(sources_analyzed[:5]))}"
        
        logger.info(f"Deep search completed with {len(items)} sources analyzed")
        print(f"Deep search completed. Analyzed {min(max_results, len(items))} sources in detail.")
        print("="*60 + "\n")
        
        return formatted_results
        
    except Exception as e:
        logger.error(f"Error during Google search: {str(e)}")
        return f"Sorry, I encountered an error while searching: {str(e)}"

async def web_search(query: str, max_results: int = 5) -> str:
    """Async wrapper for comprehensive web search with deep content extraction."""
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
    
    # If passcode is configured, use Twilio's speech recognition
    if PASSCODE:
        # Use Twilio's Gather with speech input
        gather = response.gather(
            input='speech dtmf',  # Accept both speech and DTMF
            speechModel='numbers_and_commands',  # Optimized for number recognition
            speechTimeout='1',  # Wait 3 seconds for speech
            timeout=10,
            action=f'https://{host}/verify-passcode-speech',
            method='POST',
            num_digits=len(PASSCODE),  # For DTMF fallback
            finish_on_key='#'
        )
        gather.say("Please speak or enter your passcode.")
        
        # If no input received
        response.say("No passcode received. Goodbye.")
        response.hangup()
    else:
        # No passcode required, connect directly
        # Skip Twilio greeting and let OpenAI handle the greeting
        connect = Connect()
        connect.stream(url=f'wss://{host}/media-stream')
        response.append(connect)
    
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.api_route("/verify-passcode-speech", methods=["POST"])
async def verify_passcode_speech(request: Request):
    """Verify passcode from either speech or DTMF input using Twilio."""
    form_data = await request.form()
    
    # Log all form data for debugging
    logger.info(f"Form data received: {dict(form_data)}")
    print(f"\n📋 All form data: {dict(form_data)}")
    
    speech_result = form_data.get('SpeechResult', '')
    digits = form_data.get('Digits', '')
    confidence = float(form_data.get('Confidence', '0.0'))
    attempt = int(request.query_params.get('attempt', 1))
    
    response = VoiceResponse()
    host = request.url.hostname
    
    # Check what was received - speech or DTMF
    received_input = digits if digits else speech_result
    input_type = "DTMF" if digits else "Speech"
    
    # Log the raw input for debugging
    if speech_result:
        logger.info(f"Raw speech input: '{speech_result}' (confidence: {confidence})")
        print(f"\n🎤 Raw speech heard: '{speech_result}' (confidence: {confidence:.2f})")
    
    logger.info(f"{input_type} passcode attempt {attempt}: {'*' * len(received_input)} (confidence: {confidence if not digits else 'N/A'})")
    print(f"\n📝 {input_type} input received: {'*' * len(received_input)}")
    
    # Clean up speech result - remove spaces, punctuation and convert to string
    if speech_result:
        # Remove spaces and punctuation, keep only digits
        import re
        received_input = re.sub(r'[^0-9]', '', speech_result)
        logger.info(f"Normalized speech input: '{received_input}' (expected: {'*' * len(PASSCODE)})")
        print(f"📊 After normalization: '{received_input}' (expected length: {len(PASSCODE)})")
    
    if received_input == PASSCODE:
        # Passcode correct
        logger.info(f"{input_type} passcode verified successfully")
        print(f"\n✅ {input_type} passcode verified successfully")
        
        # Connect to main assistant
        connect = Connect()
        connect.stream(url=f'wss://{host}/media-stream')
        response.append(connect)
    else:
        # Passcode incorrect
        logger.warning(f"Incorrect {input_type} passcode attempt {attempt}")
        print(f"\n❌ Incorrect {input_type} passcode attempt {attempt}/{MAX_PASSCODE_ATTEMPTS}")
        
        if attempt < MAX_PASSCODE_ATTEMPTS:
            # Give another chance
            remaining_attempts = MAX_PASSCODE_ATTEMPTS - attempt
            response.say(f"Incorrect passcode. You have {remaining_attempts} attempts remaining.")
            
            # Try again with both speech and DTMF
            gather = response.gather(
                input='speech dtmf',
                speechModel='numbers_and_commands',
                speechTimeout='3',
                timeout=10,
                action=f'https://{host}/verify-passcode-speech?attempt={attempt + 1}',
                method='POST',
                num_digits=len(PASSCODE),
                finish_on_key='#'
            )
            gather.say("Please speak or enter your passcode.")
            
            response.say("No passcode received. Goodbye.")
            response.hangup()
        else:
            # Max attempts reached
            logger.warning("Max passcode attempts reached. Hanging up.")
            print("\n🚫 Max passcode attempts reached. Hanging up.")
            response.say("Maximum attempts reached. Goodbye.")
            response.hangup()
    
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.api_route("/verify-passcode", methods=["POST"])
async def verify_passcode(request: Request):
    """Verify the passcode entered by the caller via DTMF."""
    form_data = await request.form()
    digits = form_data.get('Digits', '')
    attempt = int(request.query_params.get('attempt', 1))
    
    response = VoiceResponse()
    host = request.url.hostname
    
    # Log DTMF passcode attempt
    logger.info(f"DTMF passcode attempt {attempt}: {'*' * len(digits)} (length: {len(digits)})")
    
    if digits == PASSCODE:
        # DTMF passcode correct - connect to AI assistant
        logger.info("DTMF passcode verified successfully")
        print("\n✅ DTMF passcode verified successfully")
        
        # Skip Twilio greeting and connect directly - let OpenAI handle the greeting
        connect = Connect()
        connect.stream(url=f'wss://{host}/media-stream')
        response.append(connect)
    else:
        # DTMF passcode incorrect
        logger.warning(f"Incorrect DTMF passcode attempt {attempt}")
        print(f"\n❌ Incorrect DTMF passcode attempt {attempt}/{MAX_PASSCODE_ATTEMPTS}")
        
        if attempt < MAX_PASSCODE_ATTEMPTS:
            # Give another chance - default to voice with keypad option
            remaining_attempts = MAX_PASSCODE_ATTEMPTS - attempt
            response.say(f"Incorrect passcode. You have {remaining_attempts} attempts remaining. Please speak your passcode clearly after the beep. Press star to use the keypad instead.")
            
            connect = Connect()
            connect.stream(url=f'wss://{host}/voice-passcode-stream?attempt={attempt + 1}&allow_switch=true')
            response.append(connect)
            # After WebSocket ends, check the result
            response.redirect(f'https://{host}/voice-passcode-callback?attempt={attempt + 1}')
        else:
            # Max attempts reached - hang up
            logger.warning("Max DTMF passcode attempts reached. Hanging up.")
            print("\n🚫 Max DTMF passcode attempts reached. Hanging up.")
            
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
                                max_results = args.get('max_results', 5)
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
                                max_results = args.get('max_results', 5)
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
