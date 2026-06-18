import requests
import json
from datetime import datetime, timedelta
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import logging
from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor
import os
import tempfile
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
import re
import time
from newspaper import Article
import nltk
from readability import readability
import feedparser
import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from jinja2 import Environment, FileSystemLoader
import boto3
from botocore.config import Config
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()
try:
    from weasyprint import HTML, CSS
    WEASYPRINT_AVAILABLE = True
except OSError:
    WEASYPRINT_AVAILABLE = False
    logging.warning("WeasyPrint (GTK) not found. PDF generation will be disabled.")
except ImportError:
    WEASYPRINT_AVAILABLE = False
    logging.warning("WeasyPrint module not found.")

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Download required NLTK data (run once)
try:
    nltk.data.find('tokenizers/punkt')
except LookupError:
    nltk.download('punkt')

# Register Arabic font
try:
    pdfmetrics.registerFont(TTFont('Amiri', 'Amiri-Regular.ttf'))
except Exception as e:
    logger.error(f"Failed to register Arabic font: {e}")

# Usage limits configuration
USAGE_LIMITS = {
    'daily_news': 30,
    'weekly': 4,
    'monthly': 2,
    'magazine': 2
}

# Admin user IDs (add your Telegram user ID here)
ADMIN_USER_IDS = [1029062753]  # Add admin IDs like [123456789, 987654321]

# Usage tracking file
USAGE_FILE = 'user_usage.json'

def load_usage_data():
    """Load usage data from JSON file."""
    if os.path.exists(USAGE_FILE):
        try:
            with open(USAGE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading usage data: {e}")
            return {}
    return {}

def save_usage_data(usage_data):
    """Save usage data to JSON file."""
    try:
        with open(USAGE_FILE, 'w', encoding='utf-8') as f:
            json.dump(usage_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving usage data: {e}")

def get_user_id(update):
    """Get user ID from update."""
    if update.callback_query:
        return update.callback_query.from_user.id
    return update.message.from_user.id

def check_usage_limit(user_id, feature):
    """Check if user has reached usage limit for a feature."""
    usage_data = load_usage_data()
    user_key = str(user_id)
    
    if user_key not in usage_data:
        return True, 0  # New user, has limit
    
    user_usage = usage_data[user_key]
    feature_key = feature
    
    if feature_key not in user_usage:
        return True, 0  # Feature not used yet
    
    current_usage = user_usage[feature_key]
    limit = USAGE_LIMITS.get(feature, 0)
    
    if current_usage >= limit:
        return False, current_usage  # Limit reached
    return True, current_usage  # Still has usage left

def increment_usage(user_id, feature):
    """Increment usage count for a user and feature."""
    usage_data = load_usage_data()
    user_key = str(user_id)
    
    if user_key not in usage_data:
        usage_data[user_key] = {}
    
    if feature not in usage_data[user_key]:
        usage_data[user_key][feature] = 0
    
    usage_data[user_key][feature] += 1
    save_usage_data(usage_data)

def reset_user_usage(user_id=None):
    """Reset usage for a specific user or all users."""
    if user_id:
        usage_data = load_usage_data()
        user_key = str(user_id)
        if user_key in usage_data:
            usage_data[user_key] = {}
            save_usage_data(usage_data)
            return True
        return False
    else:
        # Reset all users
        save_usage_data({})
        return True

def get_usage_status(user_id):
    """Get current usage status for a user."""
    usage_data = load_usage_data()
    user_key = str(user_id)
    
    if user_key not in usage_data:
        return {
            'daily_news': {'used': 0, 'limit': USAGE_LIMITS['daily_news']},
            'weekly': {'used': 0, 'limit': USAGE_LIMITS['weekly']},
            'monthly': {'used': 0, 'limit': USAGE_LIMITS['monthly']},
            'magazine': {'used': 0, 'limit': USAGE_LIMITS['magazine']}
        }
    
    user_usage = usage_data[user_key]
    return {
        'daily_news': {'used': user_usage.get('daily_news', 0), 'limit': USAGE_LIMITS['daily_news']},
        'weekly': {'used': user_usage.get('weekly', 0), 'limit': USAGE_LIMITS['weekly']},
        'monthly': {'used': user_usage.get('monthly', 0), 'limit': USAGE_LIMITS['monthly']},
        'magazine': {'used': user_usage.get('magazine', 0), 'limit': USAGE_LIMITS['magazine']}
    }

# Your Telegram Bot Token (you'll need to get this from @BotFather)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8371656922:AAGcK3C4aC0lVbWFhSSzW0JS_U5hwyfLj9I")

# AWS Bedrock Configuration
# Support both variable name formats for flexibility
AWS_BEARER_TOKEN_BEDROCK = os.getenv("AWS_BEARER_TOKEN_BEDROCK")
AWS_REGION = os.getenv("AWS_REGION") or os.getenv("AWS_BEDROCK_REGION", "us-east-1")
AWS_BEDROCK_INFERENCE_PROFILE = os.getenv("AWS_BEDROCK_INFERENCE_PROFILE") or os.getenv("AWS_BEDROCK_INFERENCE_PROFILE_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

# Initialize AWS Bedrock client
try:
    if not AWS_BEARER_TOKEN_BEDROCK:
        logger.warning("âš ï¸ AWS_BEARER_TOKEN_BEDROCK environment variable is not set!")
        logger.warning("   Please set it using:")
        logger.warning("   Windows: set AWS_BEARER_TOKEN_BEDROCK=your_token")
        logger.warning("   Linux/Mac: export AWS_BEARER_TOKEN_BEDROCK=your_token")
        raise ValueError("AWS_BEARER_TOKEN_BEDROCK environment variable is required")
    
    # Standard client with default timeout (60 seconds)
    bedrock_client = boto3.client(
        service_name="bedrock-runtime",
        region_name=AWS_REGION
    )
    
    # Long-running client for magazine generation (600 seconds timeout)
    bedrock_config_long = Config(
        read_timeout=600,
        connect_timeout=10,
        retries={'max_attempts': 1}
    )
    bedrock_client_long = boto3.client(
        service_name="bedrock-runtime",
        region_name=AWS_REGION,
        config=bedrock_config_long
    )
    
    logger.info(f"âœ… AWS Bedrock client initialized successfully")
    logger.info(f"   Region: {AWS_REGION}")
    logger.info(f"   Inference Profile: {AWS_BEDROCK_INFERENCE_PROFILE}")
    logger.info(f"   Standard timeout: 60s, Long operations timeout: 600s")
except Exception as e:
    logger.error(f"âŒ Failed to initialize AWS Bedrock client: {str(e)}")
    raise

# No RSS feeds needed - using haj.gov.sa API and CNN Arabic scraping
RSS_FEEDS = []

KEYWORD_INPUT_INSTRUCTIONS = (
    "âœï¸ *Ø¥Ø¹Ø¯Ø§Ø¯ Ø§Ù„ÙƒÙ„Ù…Ø§Øª Ø§Ù„Ù…ÙØªØ§Ø­ÙŠØ© (Ø¨Ø§Ù„Ø¥Ù†Ø¬Ù„ÙŠØ²ÙŠØ©)*\n"
    "Ø£Ø±Ø³Ù„ Ø§Ù„ÙƒÙ„Ù…Ø§Øª Ø§Ù„Ù…ÙØªØ§Ø­ÙŠØ© Ø¨Ø§Ù„ØµÙŠØºØ© Ø§Ù„ØªØ§Ù„ÙŠØ© (Ø¨Ø§Ù„Ø¥Ù†Ø¬Ù„ÙŠØ²ÙŠØ©):\n"
    "`Primary Keyword | secondary keyword 1, secondary keyword 2, secondary keyword 3`\n\n"
    "Ù…Ø«Ø§Ù„:\n"
    "`Hajj News 2026 | hajj, umrah, pilgrimage, makkah`\n\n"
    "Ø£Ø±Ø³Ù„ ÙƒÙ„Ù…Ø© *cancel* Ù„Ø¥Ù„ØºØ§Ø¡ Ø¥Ø¯Ø®Ø§Ù„ Ø§Ù„ÙƒÙ„Ù…Ø§Øª Ø§Ù„Ù…ÙØªØ§Ø­ÙŠØ©."
)


def parse_keyword_input(raw_text):
    if not raw_text:
        return None
    parts = raw_text.split('|', 1)
    primary = parts[0].strip()
    if not primary:
        return None
    secondary = []
    if len(parts) > 1:
        secondary = [kw.strip() for kw in parts[1].split(',') if kw.strip()]
    return {"primary": primary, "secondary": secondary}


def format_secondary_keywords(secondary_list):
    if not secondary_list:
        return "Ù„Ù… ÙŠØªÙ… ØªØ­Ø¯ÙŠØ¯ ÙƒÙ„Ù…Ø§Øª Ø«Ø§Ù†ÙˆÙŠØ©"
    return ", ".join(secondary_list)


def build_keyword_instruction_block(keywords):
    if keywords and keywords.get("primary"):
        primary = keywords["primary"]
        secondary_text = format_secondary_keywords(keywords.get("secondary", []))
        keyword_header = (
            f'PRIMARY KEYWORD: "{primary}"\n'
            f"SECONDARY KEYWORDS / LSI: {secondary_text}\n"
        )
    else:
        keyword_header = (
            "PRIMARY KEYWORD: Not specified (infer the best fit from the Hajj and Umrah coverage)\n"
            "SECONDARY KEYWORDS / LSI: Use related Hajj, Umrah, pilgrimage terms, synonyms, and supporting subtopics\n"
        )

    return f"""
{keyword_header}
SEO requirements:
- Place the primary keyword in:
  â€¢ The SEO Title
  â€¢ The H1
  â€¢ The first paragraph (within the first 100 words)
  â€¢ Naturally 2â€“3 times every ~300 words throughout the body
- Distribute secondary/LSI keywords across select H2/H3 headings and different paragraphs as thematic synonyms.
- Do NOT repeat the exact same keyword in every headingâ€”use natural variations to avoid keyword stuffing.

Mandatory SEO outputs at the top of the response (before any other sections):
1. SEO Title: < 60 characters, includes the primary keyword and communicates a clear benefit.
2. Meta Description: 120â€“150 characters summarizing the main value, optionally includes the primary keyword once (only if it reads naturally) plus a light CTA.
3. Recommended Slug: lowercase, hyphen-separated version of the primary keyword (e.g., hajj-news-2026).
4. Headings Structure: Proposed H2/H3 outline derived from the primary + secondary keywords using varied phrasing.

After listing these SEO elements, continue with the requested Hajj news blog structure while following the keyword guidance above.
""".strip()


def keywords_summary_text(keywords):
    if not keywords or not keywords.get("primary"):
        return "Ù„Ù… ÙŠØªÙ… Ø¥Ø¹Ø¯Ø§Ø¯ Ø£ÙŠ ÙƒÙ„Ù…Ø§Øª Ù…ÙØªØ§Ø­ÙŠØ© Ø¨Ø¹Ø¯."
    secondary = format_secondary_keywords(keywords.get("secondary", []))
    return f"Ø§Ù„ÙƒÙ„Ù…Ø© Ø§Ù„Ø£Ø³Ø§Ø³ÙŠØ©: {keywords['primary']}\nØ§Ù„ÙƒÙ„Ù…Ø§Øª Ø§Ù„Ø«Ø§Ù†ÙˆÙŠØ©: {secondary}"


def get_user_keywords(context):
    try:
        return context.user_data.get("blog_keywords")
    except Exception:
        return None

def fetch_hajgov_news(page_size=50):
    """Fetch Hajj news from haj.gov.sa REST API"""
    url = "https://haj.gov.sa/s-core/customApi/News/GetAll"
    params = {
        'PageSize': page_size,
        'PageNumber': 1,
        'Language': 'ar'
    }
    articles = []
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json'
        }
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        
        items = data if isinstance(data, list) else data.get('data', data.get('items', data.get('result', [])))
        if isinstance(items, dict):
            items = items.get('items', items.get('data', []))
        
        for item in items:
            try:
                title = item.get('title', '')
                publish_date = item.get('publishDate', '')
                
                # Extract description from nested value
                desc_obj = item.get('description', {})
                description = ''
                if isinstance(desc_obj, dict):
                    description = desc_obj.get('value', '')
                elif isinstance(desc_obj, str):
                    description = desc_obj
                
                # Strip HTML tags from description
                if description:
                    description = re.sub(r'<[^>]+>', '', description).strip()
                
                # Build article URL
                item_path = item.get('itemPath', '')
                article_url = f"https://haj.gov.sa{item_path}" if item_path else ''
                
                # Get image
                image_item = item.get('imageItem', {})
                image_url = ''
                if isinstance(image_item, dict):
                    image_url = image_item.get('src', '')
                
                # Parse date
                published_iso = ''
                if publish_date:
                    try:
                        if 'T' in publish_date:
                            published_iso = publish_date
                        else:
                            dt = datetime.strptime(publish_date, '%Y-%m-%d')
                            published_iso = dt.isoformat()
                    except:
                        published_iso = publish_date
                
                article = {
                    'title': title,
                    'description': description[:500] if description else '',
                    'full_content': description,  # haj.gov.sa provides full text
                    'url': article_url,
                    'publishedAt': published_iso,
                    'source': {'name': 'ÙˆØ²Ø§Ø±Ø© Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©'},
                    'image_url': image_url,
                    'extraction_method': 'api',
                    'content_length': len(description) if description else 0
                }
                articles.append(article)
            except Exception as e:
                logger.warning(f"Error parsing haj.gov.sa article: {e}")
                continue
        
        logger.info(f"Fetched {len(articles)} articles from haj.gov.sa")
        return articles
    
    except Exception as e:
        logger.error(f"Error fetching haj.gov.sa news: {e}")
        return []

def fetch_cnn_hajj_news():
    """Scrape Hajj news from CNN Arabic tag page"""
    url = "https://arabic.cnn.com/tag/alhj"
    articles = []
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'ar,en;q=0.9'
        }
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Find article links - CNN Arabic uses various card structures
        article_links = soup.select('a[href*="/article/"]')
        
        seen_urls = set()
        for link_el in article_links:
            try:
                href = link_el.get('href', '')
                if not href or href in seen_urls:
                    continue
                
                # Build full URL
                if href.startswith('/'):
                    full_url = f"https://arabic.cnn.com{href}"
                elif href.startswith('http'):
                    full_url = href
                else:
                    continue
                
                seen_urls.add(href)
                
                # Extract title from the link text or nested elements
                title = ''
                title_el = link_el.select_one('span, h2, h3, .cd__headline-text')
                if title_el:
                    title = title_el.get_text(strip=True)
                if not title:
                    title = link_el.get_text(strip=True)
                
                if not title or len(title) < 10:
                    continue
                
                # Try to extract date from URL pattern (YYYY/MM/DD)
                published_iso = ''
                date_match = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', full_url)
                if date_match:
                    try:
                        dt = datetime(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))
                        published_iso = dt.isoformat()
                    except:
                        pass
                
                article = {
                    'title': title,
                    'description': '',
                    'url': full_url,
                    'publishedAt': published_iso,
                    'source': {'name': 'CNN Ø¹Ø±Ø¨ÙŠØ©'}
                }
                articles.append(article)
            except Exception as e:
                logger.warning(f"Error parsing CNN Arabic article: {e}")
                continue
        
        logger.info(f"Fetched {len(articles)} articles from CNN Arabic")
        return articles
    
    except Exception as e:
        logger.error(f"Error fetching CNN Arabic Hajj news: {e}")
        return []

def filter_recent_articles(articles, days=7):
    """Filter articles to only include those from the past specified days, and strictly not older than 2026"""
    if not articles:
        return []
    
    cutoff_date = datetime.now() - timedelta(days=days)
    
    # Enforce minimum date to be January 1, 2026 for the 2026 Hajj season
    min_date = datetime(2026, 1, 1)
    if cutoff_date < min_date:
        cutoff_date = min_date
        
    recent_articles = []
    
    for article in articles:
        if not article:
            continue
        published_at = article.get('publishedAt') or article.get('published_at')
        
        # Check URL or title for old years just to be extra safe
        url = article.get('url', '')
        title = article.get('title', '')
        if re.search(r'/(2025|2024|2023|2022|2021)/', url) or re.search(r'\b(2025|2024|2023|2022|2021)\b', title):
            continue
            
        if published_at:
            try:
                # Handle different date formats
                if 'T' in published_at:
                    pub_date = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
                else:
                    # Truncate to first 10 chars for robust date parsing (YYYY-MM-DD)
                    pub_date = datetime.strptime(published_at[:10], '%Y-%m-%d')
                
                if pub_date.replace(tzinfo=None) >= cutoff_date:
                    recent_articles.append(article)
            except Exception as e:
                # If date parsing fails, check if string contains old years
                pub_str = str(published_at)
                if any(old_year in pub_str for old_year in ['2025', '2024', '2023', '2022', '2021']):
                    continue
                # If it doesn't clearly contain an old year, include it
                recent_articles.append(article)
        else:
            # If no date available, include the article (already checked URL/title for old years above)
            recent_articles.append(article)
    
    return recent_articles

def categorize_articles(articles):
    """Categorize articles by Hajj and Umrah topics"""
    if not articles:
        logger.warning("No articles provided to categorize_articles")
        return {
            'Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬Ø§Ø¬': [],
            'Ø§Ù„ØªÙ†Ø¸ÙŠÙ… ÙˆØ§Ù„Ø¥Ø¯Ø§Ø±Ø©': [],
            'Ø§Ù„ØªÙ‚Ù†ÙŠØ© ÙˆØ§Ù„Ø§Ø¨ØªÙƒØ§Ø±': [],
            'Ø§Ù„ØµØ­Ø© ÙˆØ§Ù„Ø³Ù„Ø§Ù…Ø©': [],
            'Ø£Ø®Ø¨Ø§Ø± Ø¹Ø§Ù…Ø©': []
        }
    
    categories = {
        'Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬Ø§Ø¬': [],
        'Ø§Ù„ØªÙ†Ø¸ÙŠÙ… ÙˆØ§Ù„Ø¥Ø¯Ø§Ø±Ø©': [],
        'Ø§Ù„ØªÙ‚Ù†ÙŠØ© ÙˆØ§Ù„Ø§Ø¨ØªÙƒØ§Ø±': [],
        'Ø§Ù„ØµØ­Ø© ÙˆØ§Ù„Ø³Ù„Ø§Ù…Ø©': [],
        'Ø£Ø®Ø¨Ø§Ø± Ø¹Ø§Ù…Ø©': []
    }
    
    services_keywords = ['Ø®Ø¯Ù…Ø§Øª', 'Ø­Ø¬Ø§Ø¬', 'Ù…Ø¹ØªÙ…Ø±ÙŠÙ†', 'ØªÙÙˆÙŠØ¬', 'Ù†Ù‚Ù„', 'Ø¥Ø³ÙƒØ§Ù†', 'Ø³ÙƒÙ†', 'Ø¥Ø¹Ø§Ø´Ø©', 'ØªØºØ°ÙŠØ©', 'Ù…Ø®ÙŠÙ…Ø§Øª', 'Ø­Ù…Ù„Ø§Øª', 'ØªØµØ§Ø±ÙŠØ­', 'ØªØ£Ø´ÙŠØ±Ø§Øª', 'Ù†Ø³Ùƒ']
    org_keywords = ['ØªÙ†Ø¸ÙŠÙ…', 'Ø¥Ø¯Ø§Ø±Ø©', 'ÙˆØ²Ø§Ø±Ø©', 'Ù‡ÙŠØ¦Ø©', 'Ø¥Ø´Ø±Ø§Ù', 'Ø®Ø·Ø©', 'Ø§Ø³ØªØ¹Ø¯Ø§Ø¯', 'ØªØ´ØºÙŠÙ„', 'Ù…ÙˆØ³Ù…', 'Ù…Ù†Ø¸ÙˆÙ…Ø©', 'Ø·Ø§Ù‚Ø© Ø§Ø³ØªÙŠØ¹Ø§Ø¨ÙŠØ©', 'Ø£Ø¹Ø¯Ø§Ø¯']
    tech_keywords = ['ØªÙ‚Ù†ÙŠØ©', 'ØªØ·Ø¨ÙŠÙ‚', 'Ø±Ù‚Ù…ÙŠ', 'Ø°ÙƒØ§Ø¡ Ø§ØµØ·Ù†Ø§Ø¹ÙŠ', 'Ø¥Ù„ÙƒØªØ±ÙˆÙ†ÙŠ', 'Ù…Ù†ØµØ©', 'Ø±ÙˆØ¨ÙˆØª', 'Ø§Ø¨ØªÙƒØ§Ø±', 'ØªØ­ÙˆÙ„ Ø±Ù‚Ù…ÙŠ', 'smart', 'digital']
    health_keywords = ['ØµØ­Ø©', 'Ø³Ù„Ø§Ù…Ø©', 'Ø·Ø¨ÙŠ', 'Ù…Ø³ØªØ´ÙÙ‰', 'Ø¥Ø³Ø¹Ø§Ù', 'ÙˆÙ‚Ø§ÙŠØ©', 'Ø­Ø±Ø§Ø±Ø©', 'Ø¶Ø±Ø¨Ø© Ø´Ù…Ø³', 'ÙˆØ¨Ø§Ø¡', 'ØªØ·Ø¹ÙŠÙ…', 'Ù„Ù‚Ø§Ø­', 'Ø¥Ù†Ù‚Ø§Ø°', 'Ø£Ù…Ù†']
    
    for article in articles:
        if not article:
            continue
        title = article.get('title', '') or ''
        description = article.get('description', '') or ''
        full_content = article.get('full_content', '') or ''
        content = f"{title} {description} {full_content[:500]}"
        
        if any(keyword in content for keyword in services_keywords):
            categories['Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬Ø§Ø¬'].append(article)
        elif any(keyword in content for keyword in org_keywords):
            categories['Ø§Ù„ØªÙ†Ø¸ÙŠÙ… ÙˆØ§Ù„Ø¥Ø¯Ø§Ø±Ø©'].append(article)
        elif any(keyword in content for keyword in tech_keywords):
            categories['Ø§Ù„ØªÙ‚Ù†ÙŠØ© ÙˆØ§Ù„Ø§Ø¨ØªÙƒØ§Ø±'].append(article)
        elif any(keyword in content for keyword in health_keywords):
            categories['Ø§Ù„ØµØ­Ø© ÙˆØ§Ù„Ø³Ù„Ø§Ù…Ø©'].append(article)
        else:
            categories['Ø£Ø®Ø¨Ø§Ø± Ø¹Ø§Ù…Ø©'].append(article)
    
    return categories

def extract_article_content(url, max_retries=3):
    """Extract full article content from URL using multiple methods"""
    if not url or url.strip() == '':
        return None
    
    content = None

    # Special handling for NIST (National Institute of Standards and Technology)
    if 'nist.gov' in url:
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            }
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Selector for NIST full text
            content_div = soup.select_one('.text-with-summary')
            if content_div:
                # Cleanup
                for element in content_div(['script', 'style']):
                    element.decompose()
                
                text = content_div.get_text(strip=True, separator=' ')
                if len(text) > 200:
                    return {
                        'text': text,
                        'title': soup.find('title').get_text(strip=True) if soup.find('title') else '',
                        'method': 'nist_custom',
                        'publish_date': None
                    }
        except Exception as e:
            logger.warning(f"NIST custom extraction failed for {url}: {e}")
            # Fallthrough to standard methods

    # Special handling for EOS (Egyptian Organization for Standardization)
    if 'eos.org.eg' in url:
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            }
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # EOS specific selector for body
            content_div = soup.select_one('div.text-custom-text-1')
            if content_div:
                # Remove scripts/styles if any
                for element in content_div(['script', 'style']):
                    element.decompose()
                
                text = content_div.get_text(strip=True, separator=' ')
                if len(text) > 100:
                     return {
                        'text': text,
                        'title': soup.find('title').get_text(strip=True) if soup.find('title') else '',
                        'method': 'eos_custom',
                        'publish_date': None
                    }
        except Exception as e:
            logger.warning(f"EOS custom extraction failed for {url}: {e}")
            # Fallthrough to standard methods

    # Special handling for EGAC
    if 'egac.gov.eg' in url:
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            }
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Selector for EGAC full text
            content_div = soup.select_one('.new-details-container .text')
            if content_div:
                for element in content_div(['script', 'style']):
                    element.decompose()
                text = content_div.get_text(strip=True, separator=' ')
                if len(text) > 100:
                    return {
                        'text': text,
                        'title': soup.find('title').get_text(strip=True) if soup.find('title') else '',
                        'method': 'egac_custom',
                        'publish_date': None
                    }
        except Exception as e:
            logger.warning(f"EGAC custom extraction failed for {url}: {e}")

    # Method 1: Try newspaper3k first (most reliable for news articles)
    try:
        article = Article(url)
        article.download()
        article.parse()
        
        if article.text and len(article.text.strip()) > 200:
            content = {
                'text': article.text.strip(),
                'title': article.title or '',
                'authors': article.authors or [],
                'publish_date': article.publish_date,
                'method': 'newspaper3k'
            }
            logger.info(f"Successfully extracted content using newspaper3k for {url}")
            return content
    except Exception as e:
        logger.warning(f"Newspaper3k failed for {url}: {str(e)}")
    
    # Method 2: Manual web scraping with BeautifulSoup
    for attempt in range(max_retries):
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
            }
            
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Remove unwanted elements
            for element in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'ads']):
                element.decompose()
            
            # Try multiple content selectors
            content_selectors = [
                'article',
                '[role="main"]',
                '.article-content',
                '.post-content',
                '.entry-content',
                '.content',
                'main',
                '.story-body',
                '.article-body',
                '.post-body'
            ]
            
            article_text = ""
            for selector in content_selectors:
                elements = soup.select(selector)
                if elements:
                    for element in elements:
                        text = element.get_text(strip=True, separator=' ')
                        if len(text) > len(article_text):
                            article_text = text
                    break
            
            # Fallback: extract all paragraphs
            if not article_text or len(article_text) < 200:
                paragraphs = soup.find_all('p')
                article_text = ' '.join([p.get_text(strip=True) for p in paragraphs])
            
            # Clean up the text
            article_text = re.sub(r'\s+', ' ', article_text)
            article_text = article_text.strip()
            
            if article_text and len(article_text) > 200:
                content = {
                    'text': article_text,
                    'title': soup.find('title').get_text(strip=True) if soup.find('title') else '',
                    'method': 'beautifulsoup'
                }
                logger.info(f"Successfully extracted content using BeautifulSoup for {url}")
                return content
                
        except requests.RequestException as e:
            logger.warning(f"Request failed (attempt {attempt + 1}/{max_retries}) for {url}: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
        except Exception as e:
            logger.error(f"Unexpected error extracting content from {url}: {str(e)}")
            break
    
    logger.error(f"Failed to extract content from {url} after all attempts")
    return None

def enhance_articles_with_content(articles, max_articles=15, weekly_mode=False, monthly_mode=False):
    """Enhance articles with full content extraction"""
    if not articles:
        logger.warning("No articles provided to enhance_articles_with_content")
        return []
    
    enhanced_articles = []
    
    # Adjust for weekly vs daily processing
    if weekly_mode:
        max_articles = min(max_articles, 50)
        delay = 0.3
    elif monthly_mode:
        max_articles = min(max_articles, 100)
        delay = 0.5
    else:
        max_articles = min(max_articles, 20)
        delay = 0.5
    
    logger.info(f"Starting content extraction for {min(len(articles), max_articles)} articles")
    
    for i, article in enumerate(articles[:max_articles]):
        try:
            url = article.get('url', '') if article else ''
            if not url:
                continue
                
            logger.info(f"Extracting content {i+1}/{min(len(articles), max_articles)}: {url}")
            
            # Extract full content
            content_data = extract_article_content(url)
            
            # Enhance article with extracted content
            enhanced_article = article.copy()
            if content_data and content_data.get('text'):
                enhanced_article['full_content'] = content_data['text']
                enhanced_article['extraction_method'] = content_data['method']
                enhanced_article['content_length'] = len(content_data['text'])
                
                # Use extracted title if original is missing/short
                extracted_title = content_data.get('title', '')
                original_title = article.get('title', '')
                if extracted_title and original_title and len(extracted_title) > len(original_title):
                    enhanced_article['enhanced_title'] = extracted_title
            else:
                description = article.get('description', 'No content available')
                enhanced_article['full_content'] = description or 'No content available'
                enhanced_article['extraction_method'] = 'fallback'
                enhanced_article['content_length'] = len(description) if description else 0
            
            enhanced_articles.append(enhanced_article)
            
            # Respectful delay
            time.sleep(delay)
            
        except Exception as e:
            logger.error(f"Error processing article {i+1}: {str(e)}")
            # Add original article without enhancement
            enhanced_articles.append(article)
            continue
    
    logger.info(f"Content extraction completed. Enhanced {len([a for a in enhanced_articles if a.get('full_content')])} articles")
    return enhanced_articles

async def get_news(update: Update, context: ContextTypes.DEFAULT_TYPE, page=1, category=None):
    """Get today's enhanced Hajj news with presenter-style summary."""
    user_id = get_user_id(update)
    
    # Check usage limit
    has_limit, current_usage = check_usage_limit(user_id, 'daily_news')
    if not has_limit:
        limit_message = (
            f"âŒ *ØªÙ… Ø§Ù„ÙˆØµÙˆÙ„ Ø¥Ù„Ù‰ Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ù‚ØµÙ‰*\n\n"
            f"Ù„Ù‚Ø¯ Ø§Ø³ØªØ®Ø¯Ù…Øª Ø¬Ù…ÙŠØ¹ Ø§Ù„Ù…Ø­Ø§ÙˆÙ„Ø§Øª Ø§Ù„Ù…ØªØ§Ø­Ø© Ù„Ù„Ø£Ø®Ø¨Ø§Ø± Ø§Ù„ÙŠÙˆÙ…ÙŠØ© ({USAGE_LIMITS['daily_news']}/{USAGE_LIMITS['daily_news']}).\n\n"
        )
        if update.callback_query:
            await update.callback_query.answer("ØªÙ… Ø§Ù„ÙˆØµÙˆÙ„ Ø¥Ù„Ù‰ Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ù‚ØµÙ‰", show_alert=True)
            await update.callback_query.message.reply_text(limit_message, parse_mode='Markdown')
        else:
            await update.message.reply_text(limit_message, parse_mode='Markdown')
        return
    
    # Increment usage
    increment_usage(user_id, 'daily_news')
    
    # Send initial message
    if update.callback_query:
        await update.callback_query.answer()
        message = await update.callback_query.message.reply_text(
            "ðŸ•‹ Ø¬Ø§Ø±Ù ØªØ¬Ù‡ÙŠØ² Ù…ÙˆØ¬Ø² Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©...\nðŸ“– ÙŠØªÙ… Ø§Ù„Ø¢Ù† Ø¬Ù…Ø¹ Ø§Ù„Ø£Ø®Ø¨Ø§Ø± Ù…Ù† ÙˆØ²Ø§Ø±Ø© Ø§Ù„Ø­Ø¬ Ùˆ CNN Ø¹Ø±Ø¨ÙŠØ©...\nâ³ ÙŠØ±Ø¬Ù‰ Ø§Ù„Ø§Ù†ØªØ¸Ø§Ø± Ù„Ù„Ø­Ø¸Ø§Øª.",
            parse_mode='Markdown'
        )
    else:
        message = await update.message.reply_text(
            "ðŸ•‹ Ø¬Ø§Ø±Ù ØªØ¬Ù‡ÙŠØ² Ù…ÙˆØ¬Ø² Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©...\nðŸ“– ÙŠØªÙ… Ø§Ù„Ø¢Ù† Ø¬Ù…Ø¹ Ø§Ù„Ø£Ø®Ø¨Ø§Ø± Ù…Ù† ÙˆØ²Ø§Ø±Ø© Ø§Ù„Ø­Ø¬ Ùˆ CNN Ø¹Ø±Ø¨ÙŠØ©...\nâ³ ÙŠØ±Ø¬Ù‰ Ø§Ù„Ø§Ù†ØªØ¸Ø§Ø± Ù„Ù„Ø­Ø¸Ø§Øª.",
            parse_mode='Markdown'
        )
    
    try:
        # Update progress message
        await message.edit_text(
            "ðŸŒ *Ø§Ù„Ø®Ø·ÙˆØ© 1/3:* Ø¬Ù„Ø¨ Ø§Ù„Ø£Ø®Ø¨Ø§Ø± Ù…Ù† Ø§Ù„Ù…ØµØ§Ø¯Ø±...",
            parse_mode='Markdown'
        )
        
        # Fetch news from Hajj sources
        hajgov_articles = fetch_hajgov_news() or []
        cnn_articles = fetch_cnn_hajj_news() or []
        logger.info(f"Fetched {len(hajgov_articles)} haj.gov.sa, {len(cnn_articles)} CNN Arabic")
        
        # Daily scope: restrict to past 7 days
        recent_hajgov = filter_recent_articles(hajgov_articles, days=7) or []
        recent_cnn = filter_recent_articles(cnn_articles, days=7) or []
        
        await message.edit_text(
            "ðŸŒ *Ø§Ù„Ø®Ø·ÙˆØ© 2/3:* Ø§Ø³ØªØ®Ø±Ø§Ø¬ Ø§Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„ÙƒØ§Ù…Ù„ Ù„Ù„Ù…Ù‚Ø§Ù„Ø§Øª...\nðŸ“– Ù‚Ø¯ ÙŠØ³ØªØºØ±Ù‚ Ù‡Ø°Ø§ Ù…Ù† 30 Ø¥Ù„Ù‰ 60 Ø«Ø§Ù†ÙŠØ©...",
            parse_mode='Markdown'
        )
        
        # Enhance articles with full content (daily)
        enhanced_hajgov = enhance_articles_with_content(recent_hajgov, max_articles=30) or []
        enhanced_cnn = enhance_articles_with_content(recent_cnn, max_articles=20) or []
        all_enhanced_articles = enhanced_hajgov + enhanced_cnn
        
        with open("all_enhanced_hajj_articles.txt", "w", encoding="utf-8") as f:
            json.dump(all_enhanced_articles, f, ensure_ascii=False, indent=2)
        
        await message.edit_text(
            "ðŸŒ *Ø§Ù„Ø®Ø·ÙˆØ© 3/3:* Ø¥Ù†Ù‡Ø§Ø¡ Ø¥Ø¹Ø¯Ø§Ø¯ Ù…ÙˆØ¬Ø² Ø§Ù„Ø£Ø®Ø¨Ø§Ø±...",
            parse_mode='Markdown'
        )
        
        # Format the message - pass hajgov as newsapi, cnn as gnews, empty for rest
        news_message, total_pages, current_category, relevant_articles = format_news_message(
            enhanced_hajgov, enhanced_cnn, [], [], [], page, category
        )
        
        # Update message header for presenter style
        if category:
            news_message = f"ðŸ•‹ *Ù…ÙˆØ¬Ø² Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© - {category}*\n" + news_message[news_message.find('\n')+1:]
        else:
            news_message = f"ðŸ•‹ *Ù…ÙˆØ¬Ø² Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø§Ù„ÙŠÙˆÙ…ÙŠ*\n" + news_message[news_message.find('\n')+1:]
        
        # Create keyboard based on context
        keyboard = []
        
        if category:
            # Category view with pagination
            if total_pages > 1:
                nav_row = []
                if page > 1:
                    nav_row.append(InlineKeyboardButton("â¬…ï¸ Ø§Ù„Ø³Ø§Ø¨Ù‚", callback_data=f'category_{category}_{page-1}'))
                if page < total_pages:
                    nav_row.append(InlineKeyboardButton("Ø§Ù„ØªØ§Ù„ÙŠ âž¡ï¸", callback_data=f'category_{category}_{page+1}'))
                if nav_row:
                    keyboard.append(nav_row)
            
            # Add PDF download button for category
            keyboard.append([InlineKeyboardButton("ðŸ“„ ØªØ­Ù…ÙŠÙ„ ØªÙ‚Ø±ÙŠØ± Ø§Ù„Ø£Ø®Ø¨Ø§Ø±", callback_data=f'pdf_{category}')])
            keyboard.extend([
                [InlineKeyboardButton("ðŸ”„ ØªØ­Ø¯ÙŠØ« Ø¬Ø¯ÙŠØ¯", callback_data=f'category_{category}_1')],
                [InlineKeyboardButton("ðŸ  Ø§Ù„Ù‚Ø§Ø¦Ù…Ø© Ø§Ù„Ø±Ø¦ÙŠØ³ÙŠØ©", callback_data='main_menu')]
            ])
        else:
            # Main view
            keyboard = [
                [InlineKeyboardButton("ðŸ“„ ØªØ­Ù…ÙŠÙ„ Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„ÙƒØ§Ù…Ù„", callback_data='pdf_all')],
                [InlineKeyboardButton("ðŸ”„ ØªØ­Ø¯ÙŠØ« Ø¬Ø¯ÙŠØ¯", callback_data='get_news')],
                [InlineKeyboardButton("ðŸ“ ØªÙˆÙ„ÙŠØ¯ ØªÙ‚Ø§Ø±ÙŠØ± Ø£Ø³Ø¨ÙˆØ¹ÙŠØ©", callback_data='generate_weekly')],
                [InlineKeyboardButton("ðŸ  Ø§Ù„Ù‚Ø§Ø¦Ù…Ø© Ø§Ù„Ø±Ø¦ÙŠØ³ÙŠØ©", callback_data='main_menu')]
            ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Update the message with final results
        await message.edit_text(
            news_message,
            parse_mode='Markdown',
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
            
    except Exception as e:
        error_message = f"âŒ Ø­Ø¯Ø« Ø®Ø·Ø£ Ø£Ø«Ù†Ø§Ø¡ Ø¥Ø¹Ø¯Ø§Ø¯ Ù…ÙˆØ¬Ø² Ø§Ù„Ø£Ø®Ø¨Ø§Ø±: {str(e)}"
        logger.error(f"News briefing error: {str(e)}")
        await message.edit_text(error_message)

async def show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show category selection menu."""
    categories_message = """
ðŸ•‹ *ØªØµÙ†ÙŠÙØ§Øª Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©*

Ø§Ø®ØªØ± ØªØµÙ†ÙŠÙÙ‹Ø§ Ù„Ø§Ø³ØªÙƒØ´Ø§Ù Ø§Ù„Ø£Ø®Ø¨Ø§Ø± Ù…Ø¹ Ø§Ø³ØªØ®Ø±Ø§Ø¬ Ø§Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„ÙƒØ§Ù…Ù„:

â€¢ ðŸ•Œ **Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬Ø§Ø¬** - Ø§Ù„Ù†Ù‚Ù„ØŒ Ø§Ù„Ø¥Ø³ÙƒØ§Ù†ØŒ Ø§Ù„ØªÙÙˆÙŠØ¬ØŒ Ø§Ù„ØªØµØ§Ø±ÙŠØ­ØŒ Ø§Ù„Ø­Ù…Ù„Ø§Øª
â€¢ ðŸ“‹ **Ø§Ù„ØªÙ†Ø¸ÙŠÙ… ÙˆØ§Ù„Ø¥Ø¯Ø§Ø±Ø©** - Ø§Ù„ÙˆØ²Ø§Ø±Ø©ØŒ Ø§Ù„Ù‡ÙŠØ¦Ø§ØªØŒ Ø§Ù„Ø®Ø·Ø·ØŒ Ø§Ù„Ø§Ø³ØªØ¹Ø¯Ø§Ø¯Ø§Øª
â€¢ ðŸ’¡ **Ø§Ù„ØªÙ‚Ù†ÙŠØ© ÙˆØ§Ù„Ø§Ø¨ØªÙƒØ§Ø±** - Ø§Ù„ØªØ·Ø¨ÙŠÙ‚Ø§ØªØŒ Ø§Ù„Ù…Ù†ØµØ§Øª Ø§Ù„Ø±Ù‚Ù…ÙŠØ©ØŒ Ø§Ù„Ø°ÙƒØ§Ø¡ Ø§Ù„Ø§ØµØ·Ù†Ø§Ø¹ÙŠ
â€¢ ðŸ¥ **Ø§Ù„ØµØ­Ø© ÙˆØ§Ù„Ø³Ù„Ø§Ù…Ø©** - Ø§Ù„Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø·Ø¨ÙŠØ©ØŒ Ø§Ù„ÙˆÙ‚Ø§ÙŠØ©ØŒ Ø§Ù„Ø£Ù…Ù† ÙˆØ§Ù„Ø³Ù„Ø§Ù…Ø©
â€¢ ðŸ“° **Ø£Ø®Ø¨Ø§Ø± Ø¹Ø§Ù…Ø©** - Ø£Ø®Ø¨Ø§Ø± Ø­Ø¬ ÙˆØ¹Ù…Ø±Ø© Ù…ØªÙ†ÙˆØ¹Ø©

*ðŸ†• Ù…Ø²Ø§ÙŠØ§ Ù…Ø­Ø³Ù‘Ù†Ø© Ù„ÙƒÙ„ ØªØµÙ†ÙŠÙ:*
ðŸ“– Ø§Ø³ØªØ®Ø±Ø§Ø¬ Ø§Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„ÙƒØ§Ù…Ù„ Ù„Ù„Ù…Ù‚Ø§Ù„Ø§Øª
ðŸ§  Ù…Ù„Ø®ØµØ§Øª Ø°ÙƒÙŠØ© Ù…Ø®ØµØµØ© Ù„ÙƒÙ„ ØªØµÙ†ÙŠÙ
ðŸ“„ ØªÙ‚Ø§Ø±ÙŠØ± PDF Ù…ÙØµÙ„Ø© Ù…Ø¹ Ù…Ø­ØªÙˆÙ‰ ÙƒØ§Ù…Ù„
    """
    
    keyboard = [
        [InlineKeyboardButton("ðŸ•Œ Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬Ø§Ø¬", callback_data='category_Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬Ø§Ø¬_1')],
        [InlineKeyboardButton("ðŸ“‹ Ø§Ù„ØªÙ†Ø¸ÙŠÙ… ÙˆØ§Ù„Ø¥Ø¯Ø§Ø±Ø©", callback_data='category_Ø§Ù„ØªÙ†Ø¸ÙŠÙ… ÙˆØ§Ù„Ø¥Ø¯Ø§Ø±Ø©_1')],
        [InlineKeyboardButton("ðŸ’¡ Ø§Ù„ØªÙ‚Ù†ÙŠØ© ÙˆØ§Ù„Ø§Ø¨ØªÙƒØ§Ø±", callback_data='category_Ø§Ù„ØªÙ‚Ù†ÙŠØ© ÙˆØ§Ù„Ø§Ø¨ØªÙƒØ§Ø±_1')],
        [InlineKeyboardButton("ðŸ¥ Ø§Ù„ØµØ­Ø© ÙˆØ§Ù„Ø³Ù„Ø§Ù…Ø©", callback_data='category_Ø§Ù„ØµØ­Ø© ÙˆØ§Ù„Ø³Ù„Ø§Ù…Ø©_1')],
        [InlineKeyboardButton("ðŸ“° Ø£Ø®Ø¨Ø§Ø± Ø¹Ø§Ù…Ø©", callback_data='category_Ø£Ø®Ø¨Ø§Ø± Ø¹Ø§Ù…Ø©_1')],
        [InlineKeyboardButton("ðŸ  Ø§Ù„Ù‚Ø§Ø¦Ù…Ø© Ø§Ù„Ø±Ø¦ÙŠØ³ÙŠØ©", callback_data='main_menu')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Always send a new message instead of editing
    if update.callback_query:
        await update.callback_query.message.reply_text(
            categories_message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            categories_message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

def format_news_message(hajgov_articles, cnn_articles, extra_articles=None, extra2=None, extra3=None, page=1, category=None):
    """Format news for Telegram message with pagination and categories"""
    articles_per_page = 6
    all_articles = (hajgov_articles or []) + (cnn_articles or []) + (extra_articles or []) + (extra2 or []) + (extra3 or [])

    
    if category and category in ['Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬Ø§Ø¬', 'Ø§Ù„ØªÙ†Ø¸ÙŠÙ… ÙˆØ§Ù„Ø¥Ø¯Ø§Ø±Ø©', 'Ø§Ù„ØªÙ‚Ù†ÙŠØ© ÙˆØ§Ù„Ø§Ø¨ØªÙƒØ§Ø±', 'Ø§Ù„ØµØ­Ø© ÙˆØ§Ù„Ø³Ù„Ø§Ù…Ø©', 'Ø£Ø®Ø¨Ø§Ø± Ø¹Ø§Ù…Ø©']:
        # Show articles from specific category
        categorized = categorize_articles(all_articles)
        category_articles = categorized.get(category, [])
        total_pages = (len(category_articles) + articles_per_page - 1) // articles_per_page if category_articles else 1
        start_idx = (page - 1) * articles_per_page
        end_idx = start_idx + articles_per_page
        page_articles = category_articles[start_idx:end_idx]
        
        category_label = category

        message = f"ðŸ•‹ *Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© - {category_label}* (Ù…Ø­Ø³Ù‘Ù†Ø©)\n"
        message += f"ðŸ“… {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        message += f"ðŸ“„ Ø§Ù„ØµÙØ­Ø© {page} Ù…Ù† {total_pages} | Ø¹Ø¯Ø¯ Ø§Ù„Ù…Ù‚Ø§Ù„Ø§Øª: {len(category_articles)}\n"
        
        # Show content extraction stats
        enhanced_count = len([a for a in category_articles if a and a.get('full_content')])
        message += f"ðŸ“š Ù…Ù‚Ø§Ù„Ø§Øª ØªÙ… Ø§Ø³ØªØ®Ø±Ø§Ø¬ Ù…Ø­ØªÙˆØ§Ù‡Ø§ Ø¨Ø§Ù„ÙƒØ§Ù…Ù„: {enhanced_count}/{len(category_articles)}\n\n"
        
        message += f"ðŸ“° *Ø§Ù„Ù…Ù‚Ø§Ù„Ø§Øª (Ø§Ù„ØµÙØ­Ø© {page}):*\n"
        for i, article in enumerate(page_articles, start_idx + 1):
            if not article:
                continue
            title = article.get('title', 'No title')
            source = article.get('source', {}).get('name', 'Unknown') if article.get('source') else 'Unknown'
            url = article.get('url', '')
            extraction_method = article.get('extraction_method', 'N/A')
            content_length = article.get('content_length', 0)
            
            if len(title) > 65:
                title = title[:62] + "..."
            
            message += f"{i}. {title}\n"
            message += f"   ðŸ¢ Ø§Ù„Ù…ØµØ¯Ø±: {source} | ðŸ”§ Ø·Ø±ÙŠÙ‚Ø© Ø§Ù„Ø§Ø³ØªØ®Ø±Ø§Ø¬: {extraction_method}\n"
            message += f"   ðŸ“Š Ø·ÙˆÙ„ Ø§Ù„Ù…Ø­ØªÙˆÙ‰: {content_length} Ø­Ø±ÙÙ‹Ø§\n"
            if url:
                message += f"   ðŸ”— [Ù‚Ø±Ø§Ø¡Ø© Ø§Ù„ØªÙØ§ØµÙŠÙ„]({url})\n"
            message += "\n"
        
        return message, total_pages, category, category_articles
    
    else:
        # Show main summary with top articles
        message = f"â­ *ØªØ­Ø¯ÙŠØ« Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©* (Ù…Ø­Ø³Ù‘Ù† Ù…Ø¹ Ø§Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„ÙƒØ§Ù…Ù„)\n"
        message += f"ðŸ“… {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        
        # Summary Statistics
        total_articles = len(all_articles)
        enhanced_count = len([a for a in all_articles if a and a.get('full_content')])       
        # Top Articles Preview
        message += f"ðŸ“° *Ø£ÙØ¶Ù„ Ø§Ù„Ù…Ù‚Ø§Ù„Ø§Øª Ø§Ù„Ù…Ø­Ø³Ù‘Ù†Ø© Ø§Ù„ÙŠÙˆÙ…:*\n"
        
        # Show top 10 articles total
        top_articles = all_articles[:10]
        for i, article in enumerate(top_articles, 1):
            if not article:
                continue
            title = article.get('title', 'No title')
            source = article.get('source', {}).get('name', 'Unknown') if article.get('source') else 'Unknown'
            url = article.get('url', '')
            extraction_method = article.get('extraction_method', 'N/A')
            content_length = article.get('content_length', 0)
            
            if len(title) > 65:
                title = title[:62] + "..."
            
            message += f"{i}. {title}\n"
            if url:
                message += f"   ðŸ”— [Ù‚Ø±Ø§Ø¡Ø© Ø§Ù„ØªÙØ§ØµÙŠÙ„]({url})\n"
            message += "\n"
        
        return message, 1, None, all_articles

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks."""
    query = update.callback_query
    
    if query.data == 'get_news':
        await get_news(update, context)
    elif query.data == 'generate_weekly':
        await generate_weekly_blogs(update, context)
    elif query.data == 'generate_monthly':
        await generate_monthly_blogs(update, context)
    elif query.data == 'generate_magazine':
        await generate_magazine(update, context)
    elif query.data == 'show_categories':
        await show_categories(update, context)
    elif query.data == 'help':
        await help_command(update, context)
    elif query.data == 'main_menu':
        await start(update, context)
    elif query.data.startswith('pdf_'):
        # Handle PDF generation
        category = query.data.replace('pdf_', '')
        if category == 'all':
            await generate_pdf_report(update, context, None)
        else:
            await generate_pdf_report(update, context, category)
    elif query.data.startswith('category_'):
        # Handle category navigation
        parts = query.data.split('_')
        if len(parts) >= 3:
            category = '_'.join(parts[1:-1])  # Reconstruct category name
            page = int(parts[-1])
            await get_news(update, context, page, category)

async def keywords_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow the user to set or clear Hajj and Umrah-specific keywords."""
    message = update.message or update.effective_message
    if not message:
        return
    
    user_input = ''
    if context.args:
        user_input = ' '.join(context.args).strip()
    
    if user_input:
        lowered = user_input.lower()
        if lowered in ('clear', 'reset', 'remove', 'none'):
            context.user_data.pop('blog_keywords', None)
            context.user_data.pop('awaiting_keywords_input', None)
            await message.reply_text("ðŸ§¹ ØªÙ… Ù…Ø³Ø­ Ø§Ù„ÙƒÙ„Ù…Ø§Øª Ø§Ù„Ù…ÙØªØ§Ø­ÙŠØ© Ø§Ù„Ù…Ø­ÙÙˆØ¸Ø©. Ø§Ø³ØªØ®Ø¯Ù… Ø§Ù„Ø£Ù…Ø± /keywords Ù„Ø¥Ø¶Ø§ÙØ© ÙƒÙ„Ù…Ø§Øª Ø¬Ø¯ÙŠØ¯Ø© ÙÙŠ Ø£ÙŠ ÙˆÙ‚Øª.")
            return
        
        parsed_kw = parse_keyword_input(user_input)
        if parsed_kw:
            context.user_data['blog_keywords'] = parsed_kw
            context.user_data.pop('awaiting_keywords_input', None)
            await message.reply_text(f"âœ… ØªÙ… Ø­ÙØ¸ Ø§Ù„ÙƒÙ„Ù…Ø§Øª Ø§Ù„Ù…ÙØªØ§Ø­ÙŠØ©!\n{keywords_summary_text(parsed_kw)}")
        else:
            await message.reply_text(
                "âš ï¸ ÙŠØ±Ø¬Ù‰ Ø§Ø³ØªØ®Ø¯Ø§Ù… Ø§Ù„ØµÙŠØºØ© Ø§Ù„ØªØ§Ù„ÙŠØ© (Ø¨Ø§Ù„Ø¥Ù†Ø¬Ù„ÙŠØ²ÙŠØ©):\n"
                "`Primary Keyword | secondary keyword 1, secondary keyword 2`\n"
                "Ù…Ø«Ø§Ù„:\n"
                "`Hajj News 2026 | hajj, umrah, pilgrimage, makkah`",
                parse_mode='Markdown'
            )
        return
    
    context.user_data['awaiting_keywords_input'] = True
    await message.reply_text(KEYWORD_INPUT_INSTRUCTIONS, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages."""
    raw_text = (update.message.text or '').strip()
    if not raw_text:
        return
    
    if context.user_data.get('awaiting_keywords_input'):
        lowered = raw_text.lower()
        if lowered in ('cancel', 'stop', 'skip', 'exit'):
            context.user_data.pop('awaiting_keywords_input', None)
            await update.message.reply_text("ØªÙ… Ø¥Ù„ØºØ§Ø¡ Ø¥Ø¯Ø®Ø§Ù„ Ø§Ù„ÙƒÙ„Ù…Ø§Øª Ø§Ù„Ù…ÙØªØ§Ø­ÙŠØ©. Ø§Ø³ØªØ®Ø¯Ù… Ø§Ù„Ø£Ù…Ø± /keywords Ø¹Ù†Ø¯Ù…Ø§ ØªÙƒÙˆÙ† Ø¬Ø§Ù‡Ø²Ù‹Ø§.")
            return
        parsed_kw = parse_keyword_input(raw_text)
        if parsed_kw:
            context.user_data['blog_keywords'] = parsed_kw
            context.user_data.pop('awaiting_keywords_input', None)
            await update.message.reply_text(f"âœ… ØªÙ… Ø­ÙØ¸ Ø§Ù„ÙƒÙ„Ù…Ø§Øª Ø§Ù„Ù…ÙØªØ§Ø­ÙŠØ©!\n{keywords_summary_text(parsed_kw)}")
        else:
            await update.message.reply_text(
                "âš ï¸ Ù„Ù… Ø£ØªÙ…ÙƒÙ† Ù…Ù† ÙÙ‡Ù… Ø§Ù„ØµÙŠØºØ©.\n"
                "ÙŠØ±Ø¬Ù‰ Ø§Ù„Ø¥Ø±Ø³Ø§Ù„ Ø¨Ø§Ù„Ø´ÙƒÙ„ Ø§Ù„ØªØ§Ù„ÙŠ (Ø¨Ø§Ù„Ø¥Ù†Ø¬Ù„ÙŠØ²ÙŠØ©):\n"
                "`Primary Keyword | secondary keyword 1, secondary keyword 2`\n"
                "Ù…Ø«Ø§Ù„: `Hajj News 2026 | hajj, umrah, pilgrimage, makkah`",
                parse_mode='Markdown'
            )
        return
    
    if '|' in raw_text and not raw_text.startswith('/'):
        parsed_kw = parse_keyword_input(raw_text)
        if parsed_kw:
            context.user_data['blog_keywords'] = parsed_kw
            context.user_data.pop('awaiting_keywords_input', None)
            await update.message.reply_text(f"âœ… ØªÙ… Ø­ÙØ¸ Ø§Ù„ÙƒÙ„Ù…Ø§Øª Ø§Ù„Ù…ÙØªØ§Ø­ÙŠØ©!\n{keywords_summary_text(parsed_kw)}")
            return
        else:
            await update.message.reply_text(
                "âš ï¸ ÙŠØ¨Ø¯Ùˆ Ø£Ù† Ù‡Ø°Ù‡ ØµÙŠØºØ© ÙƒÙ„Ù…Ø§Øª Ù…ÙØªØ§Ø­ÙŠØ©ØŒ Ù„ÙƒÙ† Ù„Ù… Ø£ØªÙ…ÙƒÙ† Ù…Ù† ØªØ­Ù„ÙŠÙ„Ù‡Ø§.\n"
                "ÙŠØ±Ø¬Ù‰ Ø§Ù„Ø¥Ø±Ø³Ø§Ù„ Ø¨Ù‡Ø°Ù‡ Ø§Ù„ØµÙŠØºØ© (Ø¨Ø§Ù„Ø¥Ù†Ø¬Ù„ÙŠØ²ÙŠØ©): `Primary Keyword | secondary keyword 1, secondary keyword 2` "
                "Ø£Ùˆ Ø§Ø³ØªØ®Ø¯Ù… Ø§Ù„Ø£Ù…Ø± /keywords.",
                parse_mode='Markdown'
            )
            return
    
    text = raw_text.lower()
    
    if any(word in text for word in ['news', 'hajj', 'Ø­Ø¬', 'Ø¹Ù…Ø±Ø©', 'update', 'enhanced', 'Ø£Ø®Ø¨Ø§Ø±', 'Ø­Ø¬Ø§Ø¬']):
        await get_news(update, context)
    elif any(word in text for word in ['weekly', 'week', 'Ø£Ø³Ø¨ÙˆØ¹', 'Ø£Ø³Ø¨ÙˆØ¹ÙŠ']):
        await generate_weekly_blogs(update, context)
    elif any(word in text for word in ['monthly', 'month', 'Ø´Ù‡Ø±', 'Ø´Ù‡Ø±ÙŠ']):
        await generate_monthly_blogs(update, context)
    elif any(word in text for word in ['magazine', 'Ù…Ø¬Ù„Ø©', 'Ù…Ø¬Ù„Ø§Øª']):
        await generate_magazine(update, context)
    elif any(word in text for word in ['categories', 'category', 'topics', 'ØªØµÙ†ÙŠÙØ§Øª', 'ØªØµÙ†ÙŠÙ']):
        await show_categories(update, context)
    elif any(word in text for word in ['help', 'start', 'menu', 'Ù…Ø³Ø§Ø¹Ø¯Ø©', 'Ø¨Ø¯Ø§ÙŠØ©', 'Ù‚Ø§Ø¦Ù…Ø©']):
        await start(update, context)
    else:
        keyboard = [
        [InlineKeyboardButton("ðŸ“° Ø§Ù„Ù…Ù„Ø®Øµ Ø§Ù„ÙŠÙˆÙ…ÙŠ", callback_data='get_news')],
        [InlineKeyboardButton("ðŸ“Š Ø§Ù„Ù…Ù„Ø®Øµ Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹ÙŠ", callback_data='generate_weekly'),
         InlineKeyboardButton("ðŸ“… Ø§Ù„Ù…Ù„Ø®Øµ Ø§Ù„Ø´Ù‡Ø±ÙŠ", callback_data='generate_monthly')],
        [InlineKeyboardButton("ðŸ“° Ø§Ù„Ù…Ø¬Ù„Ø©", callback_data='generate_magazine')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "â­ Ø£Ù‡Ù„Ø§Ù‹ Ø¨Ùƒ! Ø£Ù†Ø§ Ø¨ÙˆØª Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø§Ù„Ù…Ø­Ø³Ù‘Ù†Ø© Ù…Ø¹ Ø§Ø³ØªØ®Ø±Ø§Ø¬ ÙƒØ§Ù…Ù„ Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„Ù…Ù‚Ø§Ù„Ø§Øª.\n\n"
            "Ø§Ø®ØªØ± Ø£Ø­Ø¯ Ø§Ù„Ø®ÙŠØ§Ø±Ø§Øª ÙÙŠ Ø§Ù„Ø£Ø³ÙÙ„ Ø£Ùˆ Ø§Ø³ØªØ®Ø¯Ù… Ù‡Ø°Ù‡ Ø§Ù„Ø£ÙˆØ§Ù…Ø±:\n"
            "â€¢ /news - Ø§Ù„Ø­ØµÙˆÙ„ Ø¹Ù„Ù‰ Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø§Ù„Ù…Ø­Ø³Ù‘Ù†Ø©\n"
            "â€¢ /weekly - ØªÙˆÙ„ÙŠØ¯ ØªÙ‚Ø§Ø±ÙŠØ±/Ù…Ø¯ÙˆÙ†Ø§Øª Ø£Ø³Ø¨ÙˆØ¹ÙŠØ©\n"
            "â€¢ /monthly - ØªÙˆÙ„ÙŠØ¯ ØªÙ‚Ø§Ø±ÙŠØ±/Ù…Ø¯ÙˆÙ†Ø§Øª Ø´Ù‡Ø±ÙŠØ©\n"
            "â€¢ /magazine - ØªÙˆÙ„ÙŠØ¯ Ù…Ø¬Ù„Ø© Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø§Ù„Ø´Ù‡Ø±ÙŠØ© (PDF)\n"
            "â€¢ /keywords - Ø¥Ø¹Ø¯Ø§Ø¯ Ø§Ù„ÙƒÙ„Ù…Ø§Øª Ø§Ù„Ù…ÙØªØ§Ø­ÙŠØ© (Ø¨Ø§Ù„Ø¥Ù†Ø¬Ù„ÙŠØ²ÙŠØ©) Ù„ØªØ­Ø³ÙŠÙ† Ù…Ø­Ø±ÙƒØ§Øª Ø§Ù„Ø¨Ø­Ø«\n"
            "â€¢ /categories - ØªØµÙØ­ Ø§Ù„Ø£Ø®Ø¨Ø§Ø± Ø­Ø³Ø¨ Ø§Ù„ØªØµÙ†ÙŠÙ\n"
            "â€¢ /help - Ø§Ù„Ù…Ø²ÙŠØ¯ Ù…Ù† Ø§Ù„Ù…Ø¹Ù„ÙˆÙ…Ø§Øª",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /start is issued."""
    welcome_message = """
â­ ðŸ‘‹ Ù…Ø±Ø­Ø¨Ø§Ù‹ Ø¨Ùƒ! Ø£Ù†Ø§ Ù…Ø³Ø§Ø¹Ø¯Ùƒ Ø§Ù„Ø¥Ø®Ø¨Ø§Ø±ÙŠ Ø§Ù„Ø°ÙƒÙŠ Ù„Ù‚Ø·Ø§Ø¹ Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©
ØªÙ… ØªØµÙ…ÙŠÙ…ÙŠ Ø®ØµÙŠØµØ§Ù‹ Ù„Ø£ÙƒÙˆÙ† Ø±ÙÙŠÙ‚Ùƒ Ø§Ù„ÙŠÙˆÙ…ÙŠ ÙÙŠ Ù…ØªØ§Ø¨Ø¹Ø© ÙƒÙ„ Ù…Ø§ ÙŠØ®Øµ Ø£Ø®Ø¨Ø§Ø± ÙˆØ®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©.
Ø£Ù‚ÙˆÙ… Ø¨Ø¬Ù…Ø¹ Ø£Ø­Ø¯Ø« Ø§Ù„Ù…Ø³ØªØ¬Ø¯Ø§ØªØŒ ØªØ­Ù„ÙŠÙ„Ù‡Ø§ØŒ ÙˆØªÙ„Ø®ÙŠØµÙ‡Ø§ Ù„Ùƒ Ø¨Ø¯Ù‚Ø© ÙˆØ§Ø­ØªØ±Ø§ÙÙŠØ© Ø¹Ø§Ù„ÙŠØ©ØŒ
Ù„ØªÙƒÙˆÙ† Ø¯Ø§Ø¦Ù…Ø§Ù‹ ÙÙŠ Ù‚Ù„Ø¨ Ø§Ù„Ø­Ø¯Ø« Ø¯ÙˆÙ† Ø¥Ù‡Ø¯Ø§Ø± ÙˆÙ‚ØªÙƒ ÙÙŠ Ø§Ù„Ø¨Ø­Ø« Ø¨ÙŠÙ† Ø§Ù„Ù…ØµØ§Ø¯Ø± Ø§Ù„Ù…ØªØ¹Ø¯Ø¯Ø©.

ðŸ¤– Ù…Ù„Ø§Ø­Ø¸Ø© Ù‡Ø§Ù…Ø©:
Ø£Ø¹ØªÙ…Ø¯ Ø¹Ù„Ù‰ Ø®ÙˆØ§Ø±Ø²Ù…ÙŠØ§Øª Ø§Ù„Ø°ÙƒØ§Ø¡ Ø§Ù„Ø§ØµØ·Ù†Ø§Ø¹ÙŠ Ø§Ù„Ù…ØªÙ‚Ø¯Ù…Ø© Ù„Ù…Ø¹Ø§Ù„Ø¬Ø© ÙˆØªÙ„Ø®ÙŠØµ Ø§Ù„Ø£Ø®Ø¨Ø§Ø±.
(Ù‡Ø°Ù‡ Ø§Ù„Ø®Ø¯Ù…Ø© ØªÙ‡Ø¯Ù Ù„ØªØ³Ù‡ÙŠÙ„ Ø§Ù„Ù…ØªØ§Ø¨Ø¹Ø© ÙˆÙ„Ø§ ØªØ¹ØªØ¨Ø± Ø¨Ø¯ÙŠÙ„Ø§Ù‹ Ø¹Ù† Ø§Ù„ØªØµØ±ÙŠØ­Ø§Øª ÙˆØ§Ù„Ù‚Ø±Ø§Ø±Ø§Øª Ø§Ù„Ø±Ø³Ù…ÙŠØ©).

âœ¨ Ø£Ø¨Ø±Ø² Ù…Ø§ Ø£ÙˆÙØ±Ù‡ Ù„Ùƒ:
ðŸ“° Ù…Ù„Ø®ØµØ§Øª ÙŠÙˆÙ…ÙŠØ© Ù„Ø£Ù‡Ù… ÙˆØ£Ø­Ø¯Ø« Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ù‚Ø·Ø§Ø¹.
ðŸ“Š ØªÙ‚Ø§Ø±ÙŠØ± ØªØ­Ù„ÙŠÙ„ÙŠØ© Ø´Ø§Ù…Ù„Ø© ÙˆÙ…ÙØµÙ„Ø© (Ø£Ø³Ø¨ÙˆØ¹ÙŠØ© ÙˆØ´Ù‡Ø±ÙŠØ©).
ðŸ“˜ Ø¥ØµØ¯Ø§Ø±Ø§Øª Ø´Ù‡Ø±ÙŠØ© Ù…ØªÙƒØ§Ù…Ù„Ø© Ø¨ØµÙŠØºØ© PDF Ø¬Ø§Ù‡Ø²Ø© Ù„Ù„Ù…Ø´Ø§Ø±ÙƒØ©.
â±ï¸ ØªÙˆÙÙŠØ± Ø§Ù„Ø¬Ù‡Ø¯ ÙˆØ§Ù„ÙˆÙ‚Øª Ù„ØªØ¨Ù‚ÙŽ Ù…Ø·Ù„Ø¹Ø§Ù‹ Ø¹Ù„Ù‰ Ù…Ø¯Ø§Ø± Ø§Ù„Ø³Ø§Ø¹Ø©.

ðŸŽ¯ Ù„Ù…Ø§Ø°Ø§ ØªØ­ØªØ§Ø¬Ù†ÙŠØŸ
â€¢ Ù„ØªÙƒÙˆÙ† Ø¹Ù„Ù‰ Ø¯Ø±Ø§ÙŠØ© ØªØ§Ù…Ø© Ø¨Ù…ØªØºÙŠØ±Ø§Øª Ø§Ù„Ø³ÙˆÙ‚ Ø¨Ø´ÙƒÙ„ ÙÙˆØ±ÙŠ.
â€¢ Ù„ØªØ²ÙˆÙŠØ¯ ÙØ±ÙŠÙ‚ Ø¹Ù…Ù„Ùƒ ÙˆØ¹Ù…Ù„Ø§Ø¦Ùƒ Ø¨ØªÙ‚Ø§Ø±ÙŠØ± Ø¯ÙˆØ±ÙŠØ© Ø§Ø­ØªØ±Ø§ÙÙŠØ© ÙˆÙ…ÙˆØ«ÙˆÙ‚Ø©.
â€¢ Ù„Ø¯Ø¹Ù… Ø§Ø¬ØªÙ…Ø§Ø¹Ø§ØªÙƒ Ø§Ù„Ø¥Ø¯Ø§Ø±ÙŠØ© Ø¨Ù…Ù„Ø®ØµØ§Øª Ø¯Ù‚ÙŠÙ‚Ø© Ø¬Ø§Ù‡Ø²Ø© Ù„Ù„Ø§Ø³ØªØ®Ø¯Ø§Ù….

ðŸš€ Ø¬Ø§Ù‡Ø² Ù„Ù„Ø¨Ø¯Ø¡ØŸ
Ø§Ø³ØªØ®Ø¯Ù… Ø§Ù„Ø®ÙŠØ§Ø±Ø§Øª ÙˆØ§Ù„Ø£Ø²Ø±Ø§Ø± Ø¨Ø§Ù„Ø£Ø³ÙÙ„ Ù„Ø§Ø³ØªÙƒØ´Ø§Ù Ø§Ù„Ø£Ø®Ø¨Ø§Ø± ÙˆØ§Ù„ØªÙ‚Ø§Ø±ÙŠØ±.
    """
    
    keyboard = [
        [InlineKeyboardButton("â­ Ø§Ù„Ù…Ù„Ø®Øµ Ø§Ù„ÙŠÙˆÙ…ÙŠ", callback_data='get_news')],
        [InlineKeyboardButton("ðŸ“Š Ø§Ù„ØªØµÙ†ÙŠÙØ§Øª", callback_data='show_categories')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Handle both regular messages and callback queries
    if update.callback_query:
        await update.callback_query.message.reply_text(
            welcome_message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            welcome_message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

def call_claude_api(system_message, user_message, api_key=None, model=None, max_tokens=16384, temperature=0.7, use_cache=True, use_long_timeout=False):
    """
    Helper function to call AWS Bedrock Claude API.
    Maintains the same signature as the old API function for compatibility.
    
    Args:
        system_message: The system prompt
        user_message: The user prompt
        api_key: Not used (kept for compatibility), uses global client
        model: Not used (kept for compatibility), uses global inference profile
        max_tokens: Maximum tokens in response (default: 16384)
        temperature: Temperature setting (0.0-1.0)
        use_cache: Not used (kept for compatibility)
        use_long_timeout: If True, use 600s timeout (for long operations like magazine generation)
    
    Returns:
        tuple: (response_text, error_message) - error_message is None if successful
    """
    try:
        # Build messages array for AWS Bedrock Claude
        # Combine system and user message since Bedrock Claude uses a simple format
        # We'll prepend the system message to the user message for compatibility
        combined_content = user_message
        if system_message:
            combined_content = f"{system_message}\n\n{user_message}"
        
        messages = [
            {
                "role": "user",
                "content": combined_content
            }
        ]
        
        # Build request body for Bedrock Claude
        # Claude Sonnet 4.5 supports up to 64K output tokens
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": min(max_tokens, 64000),  # Claude Sonnet 4.5 supports up to 64K output
            "temperature": temperature,
            "messages": messages
        }
        
        # Select client based on timeout requirement
        client_to_use = bedrock_client_long if use_long_timeout else bedrock_client
        timeout_info = "600s (long operation)" if use_long_timeout else "60s (standard)"
        
        logger.info(f"Making AWS Bedrock API call - Inference Profile: {AWS_BEDROCK_INFERENCE_PROFILE}, Timeout: {timeout_info}")
        
        # Call AWS Bedrock API
        response = client_to_use.invoke_model(
            modelId=AWS_BEDROCK_INFERENCE_PROFILE,
            body=json.dumps(body)
        )
        
        logger.info(f"AWS Bedrock API call successful")
        
        # Parse response
        response_body = json.loads(response["body"].read())
        
        # Log stop reason for debugging
        stop_reason = response_body.get("stop_reason", "unknown")
        logger.info(f"AWS Bedrock response stop_reason: {stop_reason}")
        
        # Extract content from response
        if response_body.get("content") and len(response_body["content"]) > 0:
            content_text = response_body["content"][0].get("text", "")
            
            if not content_text:
                return None, "AWS Bedrock returned empty content"
            
            # Warn if response was truncated due to max_tokens
            if stop_reason == "max_tokens":
                logger.warning("âš ï¸ Response was truncated due to max_tokens limit!")
                logger.warning("   The model hit the output token limit before completing the response.")
                logger.warning("   Consider reducing the complexity of the request or splitting into multiple calls.")
            
            return content_text, None
        else:
            return None, "AWS Bedrock returned no content in response"
    
    except Exception as e:
        # Log the full exception for debugging
        error_type = type(e).__name__
        error_str = str(e)
        logger.error(f"AWS Bedrock API error: {error_type}: {error_str}")
        
        error_msg = error_str
        
        # Try to extract more specific error information from AWS Bedrock exceptions
        if hasattr(e, 'response'):
            try:
                if hasattr(e.response, 'get'):
                    error_data = e.response.get('Error', {})
                    if error_data:
                        error_msg = error_data.get('Message', error_msg)
                        error_code = error_data.get('Code', '')
                        logger.error(f"AWS Bedrock error code: {error_code}")
            except:
                pass
        
        # Check for specific error types
        if "ThrottlingException" in error_str or "Too many tokens" in error_str or "throttling" in error_str.lower():
            logger.error("âš ï¸ Rate limit exceeded - you've hit your daily token limit")
            logger.error("   Solutions:")
            logger.error("   1. Wait until your quota resets (usually daily)")
            logger.error("   2. Check your AWS Bedrock quotas: https://console.aws.amazon.com/servicequotas/")
            logger.error("   3. Request a quota increase if needed")
            logger.error("   4. Try a different model that has available quota")
            error_msg = f"Rate limit exceeded: {error_msg}"
        elif "ValidationException" in error_str or "validation" in error_str.lower():
            logger.error("âš ï¸ Validation error - check inference profile configuration")
            logger.error(f"   Inference Profile: {AWS_BEDROCK_INFERENCE_PROFILE}")
            logger.error(f"   Region: {AWS_REGION}")
            error_msg = f"Validation error: {error_msg}"
        elif "401" in error_str or "authentication" in error_str.lower() or "unauthorized" in error_str.lower():
            logger.error("âš ï¸ Authentication error - check AWS_BEARER_TOKEN_BEDROCK environment variable")
            error_msg = f"Authentication error: {error_msg}"
        
        return None, f"AWS Bedrock Error: {error_msg}"

def categorize_articles_for_blogs(articles):
    """Categorize Hajj and Umrah articles into two main blog themes"""
    
    if not articles:
        logger.warning("No articles provided to categorize_articles_for_blogs")
        return {
            'management': [],
            'improvement': []
        }
    
    # Blog 1: Pilgrim Services & Organization
    management_keywords = [
        'Ø®Ø¯Ù…Ø§Øª', 'Ø­Ø¬Ø§Ø¬', 'Ù…Ø¹ØªÙ…Ø±ÙŠÙ†', 'ØªÙÙˆÙŠØ¬', 'Ù†Ù‚Ù„', 'Ø¥Ø³ÙƒØ§Ù†', 'Ø³ÙƒÙ†',
        'Ø¥Ø¹Ø§Ø´Ø©', 'ØªØºØ°ÙŠØ©', 'Ù…Ø®ÙŠÙ…Ø§Øª', 'Ø­Ù…Ù„Ø§Øª', 'ØªØµØ§Ø±ÙŠØ­', 'ØªØ£Ø´ÙŠØ±Ø§Øª',
        'Ù†Ø³Ùƒ', 'ØªÙ†Ø¸ÙŠÙ…', 'Ø¥Ø¯Ø§Ø±Ø©', 'ÙˆØ²Ø§Ø±Ø©', 'Ù‡ÙŠØ¦Ø©', 'Ø¥Ø´Ø±Ø§Ù', 'Ø®Ø·Ø©'
    ]
    
    # Blog 2: Technology, Health & Innovation
    improvement_keywords = [
        'ØªÙ‚Ù†ÙŠØ©', 'ØªØ·Ø¨ÙŠÙ‚', 'Ø±Ù‚Ù…ÙŠ', 'Ø°ÙƒØ§Ø¡ Ø§ØµØ·Ù†Ø§Ø¹ÙŠ', 'Ø¥Ù„ÙƒØªØ±ÙˆÙ†ÙŠ', 'Ù…Ù†ØµØ©',
        'ØµØ­Ø©', 'Ø³Ù„Ø§Ù…Ø©', 'Ø·Ø¨ÙŠ', 'Ù…Ø³ØªØ´ÙÙ‰', 'Ø¥Ø³Ø¹Ø§Ù', 'ÙˆÙ‚Ø§ÙŠØ©',
        'Ø§Ø¨ØªÙƒØ§Ø±', 'ØªØ­ÙˆÙ„ Ø±Ù‚Ù…ÙŠ', 'ØªØ·ÙˆÙŠØ±', 'ØªØ­Ø³ÙŠÙ†', 'Ø£Ù…Ù†'
    ]
    
    management_articles = []
    improvement_articles = []
    general_articles = []
    
    for article in articles:
        if not article:
            continue
        title = article.get('title', '') or ''
        description = article.get('description', '') or ''
        full_content = article.get('full_content', '') or ''
        content = f"{title.lower()} {description.lower()} {full_content.lower()[:1000]}"
        
        management_score = sum(1 for keyword in management_keywords if keyword in content)
        improvement_score = sum(1 for keyword in improvement_keywords if keyword in content)
        
        if management_score > improvement_score and management_score > 0:
            management_articles.append(article)
        elif improvement_score > 0:
            improvement_articles.append(article)
        else:
            general_articles.append(article)
    
    # Distribute general articles
    half_general = len(general_articles) // 2
    management_articles.extend(general_articles[:half_general])
    improvement_articles.extend(general_articles[half_general:])
    
    return {
        'management': management_articles,
        'improvement': improvement_articles
    }

def parse_blog_sections(blog_content):
    """Parse blog content and return structured sections"""
    if not blog_content:
        logger.warning("No blog content provided to parse_blog_sections")
        return []
    
    sections = []
    current_section = {"title": "", "content": "", "level": 0}
    
    lines = blog_content.split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Check for headers
        if line.startswith('#'):
            # Save previous section if it has content
            if current_section["content"].strip():
                sections.append(current_section.copy())
            
            # Start new section
            level = len(line) - len(line.lstrip('#'))
            title = line.lstrip('#').strip()
            
            current_section = {
                "title": title,
                "content": "",
                "level": level
            }
        else:
            # Add to current section content
            current_section["content"] += line + " "
    
    # Don't forget the last section
    if current_section["content"].strip():
        sections.append(current_section)
    
    return sections

def process_arabic_text(text):
    """Reshape and reorder Arabic text for correct display in PDF"""
    if not text:
        return ""
    reshaped_text = arabic_reshaper.reshape(text)
    bidi_text = get_display(reshaped_text)
    return bidi_text

def repair_mojibake_text(text):
    """Repair Arabic text that was accidentally decoded as Windows-1252."""
    if not text:
        return text
    try:
        raw = bytearray()
        for ch in str(text):
            try:
                raw.extend(ch.encode("cp1252"))
            except UnicodeEncodeError:
                codepoint = ord(ch)
                if codepoint <= 255:
                    raw.append(codepoint)
                else:
                    return text
        return bytes(raw).decode("utf-8")
    except UnicodeError:
        return text

def create_hajj_blog_pdf(blog_content, blog_title, is_temp_file=True):
    """Create a beautifully formatted Hajj and Umrah blog-style PDF"""
    
    if not blog_content or not blog_title:
        logger.warning("No blog content or title provided to create_hajj_blog_pdf")
        return None
    
    if is_temp_file:
        # Create a temporary file for Telegram bot
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
        filename = temp_file.name
        temp_file.close()
    else:
        # Create with specific filename for standalone use
        filename = f"{blog_title.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}.pdf"
    
    doc = SimpleDocTemplate(filename, pagesize=A4, 
                          topMargin=0.75*inch, bottomMargin=0.75*inch,
                          leftMargin=0.75*inch, rightMargin=0.75*inch)
    
    # Define comprehensive blog styles
    styles = getSampleStyleSheet()
    
    # Blog title style (main headline)
    blog_title_style = ParagraphStyle(
        'BlogTitle',
        parent=styles['Heading1'],
        fontSize=28,
        spaceAfter=15,
        spaceBefore=0,
        alignment=TA_CENTER,
        textColor=HexColor('#1a1a1a'),
        fontName='Amiri',
        leading=32
    )
    
    # Blog metadata style (date, info)
    blog_meta_style = ParagraphStyle(
        'BlogMeta',
        parent=styles['Normal'],
        fontSize=11,
        spaceAfter=30,
        alignment=TA_CENTER,
        textColor=HexColor('#666666'),
        fontName='Amiri'
    )
    
    # Section header style (H2)
    section_header_style = ParagraphStyle(
        'SectionHeader',
        parent=styles['Heading2'],
        fontSize=18,
        spaceAfter=12,
        spaceBefore=24,
        textColor=HexColor('#2c3e50'),
        fontName='Amiri',
        alignment=TA_RIGHT
    )
    
    # Subsection header style (H3)
    subsection_header_style = ParagraphStyle(
        'SubsectionHeader',
        parent=styles['Heading3'],
        fontSize=14,
        spaceAfter=8,
        spaceBefore=16,
        textColor=HexColor('#34495e'),
        fontName='Amiri',
        alignment=TA_RIGHT
    )
    
    # Blog paragraph style
    blog_paragraph_style = ParagraphStyle(
        'BlogParagraph',
        parent=styles['Normal'],
        fontSize=12,
        spaceAfter=12,
        spaceBefore=0,
        alignment=TA_RIGHT,
        leading=18,
        textColor=HexColor('#333333'),
        fontName='Amiri'
    )
    
    # Build the document content
    content = []
    
    # Add logo at the top right corner if available
    template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
    logo_path = os.path.join(template_dir, 'images', 'Logo.png')
    if os.path.exists(logo_path):
        try:
            # Calculate available width (A4 width - left margin - right margin)
            available_width = A4[0] - (0.75*inch * 2)  # A4 width minus margins
            # Add logo with bigger size (5 inches wide, maintain aspect ratio) in top right corner
            logo = Image(logo_path, width=5*inch, height=1.25*inch, kind='proportional')
            # Use Table to position logo in top right corner
            logo_table = Table([[logo]], colWidths=[available_width])
            logo_table.setStyle(TableStyle([
                ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ]))
            content.append(logo_table)
            content.append(Spacer(1, 10))
        except Exception as e:
            logger.warning(f"Could not add logo to PDF: {str(e)}")
    
    # Add blog title
    blog_title = repair_mojibake_text(blog_title)
    content.append(Paragraph(process_arabic_text(blog_title), blog_title_style))
    
    # Add metadata
    date_str = datetime.now().strftime('%B %d, %Y')
    week_range = f"{(datetime.now() - timedelta(days=7)).strftime('%B %d')} - {datetime.now().strftime('%B %d, %Y')}"
    month_range = f"{(datetime.now() - timedelta(days=30)).strftime('%B %d')} - {datetime.now().strftime('%B %d, %Y')}"
    
    if 'ÙŠÙˆÙ…ÙŠ' in blog_title:
        meta_text = f"ØªÙ‚Ø±ÙŠØ± Ø³ÙŠØ§Ø­ÙŠ ÙŠÙˆÙ…ÙŠ â€¢ ØªÙ… Ø§Ù„Ø¥Ù†Ø´Ø§Ø¡ ÙÙŠ {date_str}"
    elif 'Ø£Ø³Ø¨ÙˆØ¹ÙŠ' in blog_title:
        meta_text = f"ØªÙ‚Ø±ÙŠØ± Ø³ÙŠØ§Ø­ÙŠ Ø£Ø³Ø¨ÙˆØ¹ÙŠ â€¢ {week_range} â€¢ ØªÙ… Ø§Ù„Ø¥Ù†Ø´Ø§Ø¡ ÙÙŠ {date_str}"
    elif 'Ø´Ù‡Ø±ÙŠ' in blog_title:
        meta_text = f"ØªÙ‚Ø±ÙŠØ± Ø³ÙŠØ§Ø­ÙŠ Ø´Ù‡Ø±ÙŠ â€¢ {month_range} â€¢ ØªÙ… Ø§Ù„Ø¥Ù†Ø´Ø§Ø¡ ÙÙŠ {date_str}"
    else:
        meta_text = f"ØªÙ‚Ø±ÙŠØ± Ø³ÙŠØ§Ø­ÙŠ â€¢ ØªÙ… Ø§Ù„Ø¥Ù†Ø´Ø§Ø¡ ÙÙŠ {date_str}"
    meta_text = repair_mojibake_text(meta_text)
    content.append(Paragraph(process_arabic_text(meta_text), blog_meta_style))
    
    content.append(Spacer(1, 10))
    
    # Parse the blog content into sections
    sections = parse_blog_sections(blog_content)
    
    for section in sections:
        title = section['title']
        section_content = section['content'].strip()
        level = section['level']
        
        # Skip empty sections
        if not section_content:
            continue
        
        # Add section header based on level
        if level == 1:
            continue  # Main title already added
        elif level == 2:
            if title:
                content.append(Paragraph(process_arabic_text(title), section_header_style))
        elif level == 3:
            if title:
                content.append(Paragraph(process_arabic_text(title), subsection_header_style))
        
        # Add section content
        if section_content:
            # Split into paragraphs
            paragraphs = section_content.split('. ')
            for para in paragraphs:
                para = para.strip()
                if para and len(para) > 20:
                    # Close the sentence if not ending with punctuation
                    if not para.endswith('.'):
                        para += '.'
                    content.append(Paragraph(process_arabic_text(para), blog_paragraph_style))
    
    # Build the PDF
    try:
        doc.build(content)
        logger.info(f"Successfully created Hajj news blog PDF: {filename}")
        return filename
    except Exception as e:
        logger.error(f"Error creating Hajj news blog PDF: {str(e)}")
        return None

async def generate_pdf_report(update: Update, context: ContextTypes.DEFAULT_TYPE, category=None):
    """Generate and send enhanced PDF report with full content in daily blog style."""
    query = update.callback_query
    await query.answer()
    
    # Send status message
    status_message = await query.message.reply_text(
        "ðŸ“„ Ø¬Ø§Ø±Ù ØªÙˆÙ„ÙŠØ¯ ØªÙ‚Ø±ÙŠØ± PDF Ù…Ø­Ø³Ù‘Ù†...\nðŸ“– ÙŠØªÙ… Ø§Ù„Ø¢Ù† Ø§Ø³ØªØ®Ø±Ø§Ø¬ Ø§Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„ÙƒØ§Ù…Ù„ Ù„ØªØ­Ù„ÙŠÙ„ Ø£ÙƒØ«Ø± ØªÙØµÙŠÙ„Ø§Ù‹...\nâ³ ÙŠØ±Ø¬Ù‰ Ø§Ù„Ø§Ù†ØªØ¸Ø§Ø± Ù…Ù† 1 Ø¥Ù„Ù‰ 2 Ø¯Ù‚ÙŠÙ‚Ø©.",
        parse_mode='Markdown'
    )
    
    try:
        await status_message.edit_text(
            "ðŸ“„ *Ø§Ù„Ø®Ø·ÙˆØ© 1/2:* ØªÙˆÙ„ÙŠØ¯ Ù…Ø­ØªÙˆÙ‰ ØªÙ‚Ø±ÙŠØ±ÙŠ Ø¨Ø£Ø³Ù„ÙˆØ¨ Ù…Ø¯ÙˆÙ†Ø© Ø³ÙŠØ§Ø­ÙŠØ©...",
            parse_mode='Markdown'
        )
        
        # Load articles from saved file
        if not os.path.exists("all_enhanced_hajj_articles.txt"):
            await status_message.edit_text(
                "âŒ Ù„Ø§ ØªÙˆØ¬Ø¯ Ù…Ù‚Ø§Ù„Ø§Øª Ù…ØªØ§Ø­Ø© Ø­Ø§Ù„ÙŠÙ‹Ø§. ÙŠØ±Ø¬Ù‰ ØªØ´ØºÙŠÙ„ Ø§Ù„Ø£Ù…Ø± /news Ø£ÙˆÙ„Ø§Ù‹ Ù„Ø¬Ù„Ø¨ Ø§Ù„Ù…Ù‚Ø§Ù„Ø§Øª.",
                parse_mode='Markdown'
            )
            return
        
        with open("all_enhanced_hajj_articles.txt", "r", encoding="utf-8") as f:
            all_articles = json.load(f)
        
        # Prepare articles according to scope
        user_keywords = get_user_keywords(context)
        if category and category != 'all':
            categorized = categorize_articles(all_articles)
            articles_for_report = categorized.get(category, [])
            blog_title = f"ØªÙ‚Ø±ÙŠØ± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø§Ù„ÙŠÙˆÙ…ÙŠ â€“ {category}"
            blog_content = generate_daily_hajj_blog_with_ai(articles_for_report, category, keywords=user_keywords)
        else:
            articles_for_report = all_articles
            blog_title = "ØªÙ‚Ø±ÙŠØ± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø§Ù„ÙŠÙˆÙ…ÙŠ"
            blog_content = generate_daily_hajj_blog_with_ai(articles_for_report, None, keywords=user_keywords)

        # Fallback if model returned too-short, empty content, or error message
        if not blog_content or len(blog_content.strip()) < 100 or (blog_content.startswith("# Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„ÙŠÙˆÙ…ÙŠ Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©") and "Error" in blog_content):
            logger.warning("Model returned empty/short content or error. Using fallback blog content.")
            blog_content = build_fallback_hajj_blog_content(articles_for_report, category)
        
        # Build PDF using blog formatter for consistent look
        pdf_filename = create_hajj_blog_pdf(blog_content, blog_title, is_temp_file=True)
        report_title = blog_title
        
        await status_message.edit_text(
            "ðŸ“„ *Ø§Ù„Ø®Ø·ÙˆØ© 2/2:* Ø¥Ù†Ø´Ø§Ø¡ Ù…Ù„Ù PDF...",
            parse_mode='Markdown'
        )
        
        # Send the PDF file
        if pdf_filename and os.path.exists(pdf_filename):
            with open(pdf_filename, 'rb') as pdf_file:
                await query.message.reply_document(
                    document=pdf_file,
                    filename=f"{report_title.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}.pdf",
                    caption=f"ðŸ“„ *{report_title}*\nðŸ“… ØªØ§Ø±ÙŠØ® Ø§Ù„Ø¥Ù†Ø´Ø§Ø¡: {datetime.now().strftime('%Y-%m-%d %H:%M')}\nðŸ“– ØªÙ‚Ø±ÙŠØ± Ù…Ø­Ø³Ù‘Ù† Ù…Ø¹ Ø§Ø³ØªØ®Ø±Ø§Ø¬ ÙƒØ§Ù…Ù„ Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„Ù…Ù‚Ø§Ù„Ø§Øª",
                    parse_mode='Markdown'
                )
            
            # Clean up the temporary file
            os.unlink(pdf_filename)
            
            # Update status message
            await status_message.edit_text(
                "âœ… ØªÙ… ØªÙˆÙ„ÙŠØ¯ ØªÙ‚Ø±ÙŠØ± PDF Ù…Ø­Ø³Ù‘Ù† ÙˆØ¥Ø±Ø³Ø§Ù„Ù‡ Ø¨Ù†Ø¬Ø§Ø­!\nðŸ“– ÙŠØ´Ù…Ù„ Ù…Ø­ØªÙˆÙ‰ ÙƒØ§Ù…Ù„Ù‹Ø§ Ù„Ù„Ù…Ù‚Ø§Ù„Ø§Øª ÙˆØªØ­Ù„ÙŠÙ„Ù‹Ø§ Ø³ÙŠØ§Ø­ÙŠÙ‹Ø§ ØªÙØµÙŠÙ„ÙŠÙ‹Ø§.",
                parse_mode='Markdown'
            )
        else:
            await status_message.edit_text(
                "âŒ Ø®Ø·Ø£: ØªØ¹Ø°Ø± Ø¥Ù†Ø´Ø§Ø¡ Ù…Ù„Ù PDF.",
                parse_mode='Markdown'
            )
        
    except Exception as e:
        try:
            await status_message.edit_text(
                f"âŒ Ø­Ø¯Ø« Ø®Ø·Ø£ Ø£Ø«Ù†Ø§Ø¡ ØªÙˆÙ„ÙŠØ¯ ØªÙ‚Ø±ÙŠØ± PDF Ø§Ù„Ù…Ø­Ø³Ù‘Ù†: {str(e)}",
                parse_mode='Markdown'
            )
        except Exception as edit_error:
            # Try to send a new message instead
            try:
                await status_message.reply_text(
                    f"âŒ Ø­Ø¯Ø« Ø®Ø·Ø£ Ø£Ø«Ù†Ø§Ø¡ ØªÙˆÙ„ÙŠØ¯ ØªÙ‚Ø±ÙŠØ± PDF Ø§Ù„Ù…Ø­Ø³Ù‘Ù†: {str(e)}"
                )
            except Exception as reply_error:
                logger.error(f"Error sending reply: {reply_error}")

def generate_daily_hajj_blog_with_ai(articles, category=None, keywords=None):
    """Generate a daily Hajj and Umrah blog-style summary so PDFs match weekly blog formatting."""
    if not articles:
        logger.warning("No articles provided to generate_daily_hajj_blog_with_ai")
        return "# Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„ÙŠÙˆÙ…ÙŠ Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©\n\nÙ„Ø§ ØªÙˆØ¬Ø¯ Ù…Ù‚Ø§Ù„Ø§Øª Ù…ØªØ§Ø­Ø© Ø§Ù„ÙŠÙˆÙ…."
    
    # Prepare content from articles (shorter excerpts for daily)
    max_daily_articles = min(len(articles), 40)
    news_content = ""
    for i, article in enumerate(articles[:max_daily_articles], 1):
        if not article:
            continue
        title = article.get('title', 'No title')
        source = article.get('source', {}).get('name', 'Unknown source') if article.get('source') else 'Unknown source'
        full_content = article.get('full_content', article.get('description', 'No content'))
        published_date = article.get('publishedAt', 'Unknown date')
        url = article.get('url', '')
        if full_content and len(full_content) > 450:
            full_content = full_content[:450] + "..."
        news_content += f"""
ARTICLE {i}:
Title: {title}
Source: {source}
Date: {published_date}
URL: {url}
Content: {full_content or 'No content available'}
---
"""
    
    # Choose focus
    if category in [
        "Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬Ø§Ø¬",
        "Ø§Ù„ØªÙ†Ø¸ÙŠÙ… ÙˆØ§Ù„Ø¥Ø¯Ø§Ø±Ø©",
        "Ø§Ù„ØªÙ‚Ù†ÙŠØ© ÙˆØ§Ù„Ø§Ø¨ØªÙƒØ§Ø±",
        "Ø§Ù„ØµØ­Ø© ÙˆØ§Ù„Ø³Ù„Ø§Ù…Ø©",
        "Ø£Ø®Ø¨Ø§Ø± Ø¹Ø§Ù…Ø©"
    ]:
        title_suffix = f" â€“ {category}"
        intro_target = f"Ø£Ù‡Ù… ØªØ·ÙˆØ±Ø§Øª {category} Ø§Ù„ÙŠÙˆÙ…"
    else:
        title_suffix = ""
        intro_target = "Ø£Ù‡Ù… ØªØ·ÙˆØ±Ø§Øª Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø§Ù„ÙŠÙˆÙ…"
    
    # System message (static, will be cached)
    system_message = (
        "You are a professional Arabic writer. "
        "You write concise, structured daily blog reports about Hajj, Umrah, and pilgrimage news in MODERN STANDARD ARABIC. "
        "All visible content, headings, and paragraphs must be in Arabic, but you may read/analyze English source text. "
        "Keep the style ØµØ­ÙÙŠ Ø§Ø­ØªØ±Ø§ÙÙŠ ÙˆØ³Ù‡Ù„ Ø§Ù„Ù‚Ø±Ø§Ø¡Ø©ØŒ ÙˆØ§Ø³ØªØ®Ø¯Ù… Ø¹Ù†Ø§ÙˆÙŠÙ† Markdown."
    )
    
    keyword_guidance = build_keyword_instruction_block(keywords)
    
    user_prompt = f"""
    {keyword_guidance}

    Ø§ÙƒØªØ¨ ØªÙ‚Ø±ÙŠØ±Ù‹Ø§ ÙŠÙˆÙ…ÙŠÙ‹Ø§ Ù…ÙˆØ¬Ø²Ù‹Ø§ Ø¨Ø£Ø³Ù„ÙˆØ¨ Ù…Ø¯ÙˆÙ†Ø© Ø¹Ù† {intro_target} Ø¨Ø§Ù„Ù„ØºØ© Ø§Ù„Ø¹Ø±Ø¨ÙŠØ© Ø§Ù„ÙØµØ­Ù‰ØŒ
    Ù…Ø³ØªØ®Ø¯Ù…Ù‹Ø§ Ø§Ù„Ø¨Ù†ÙŠØ© Ø§Ù„ØªØ§Ù„ÙŠØ© **Ø¨Ø§Ù„Ø¶Ø¨Ø·** Ø¨Ø§Ø³ØªØ®Ø¯Ø§Ù… Markdown. Ø§Ø¬Ø¹Ù„ Ø§Ù„Ù†Øµ Ù…Ø±ÙƒØ²Ù‹Ø§ ÙˆØºÙ†ÙŠÙ‹Ø§ Ø¨Ø§Ù„Ù…Ø¹Ù„ÙˆÙ…Ø§Øª.

# [Ø§ÙƒØªØ¨ Ø¹Ù†ÙˆØ§Ù†Ù‹Ø§ Ø¹Ø±Ø¨ÙŠÙ‹Ø§ Ø¬Ø°Ø§Ø¨Ù‹Ø§ Ù„Ù„ÙŠÙˆÙ…]

## Ù†Ø¸Ø±Ø© Ø³Ø±ÙŠØ¹Ø©
[ÙÙ‚Ø±Ø© Ù…Ù† 80â€“120 ÙƒÙ„Ù…Ø© ØªÙ„Ø®Øµ Ø£Ù‡Ù… Ù…Ø­Ø§ÙˆØ± Ø§Ù„ÙŠÙˆÙ… ÙˆØ§Ù„Ø¹Ù†Ø§ÙˆÙŠÙ† Ø§Ù„Ø±Ø¦ÙŠØ³ÙŠØ© ÙÙŠ Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©]

## Ø£Ø¨Ø±Ø² Ø§Ù„Ø£Ø®Ø¨Ø§Ø±
[2-3 ÙÙ‚Ø±Ø§Øª Ù‚ØµÙŠØ±Ø©ØŒ ÙƒÙ„ Ù…Ù†Ù‡Ø§ 80â€“120 ÙƒÙ„Ù…Ø©ØŒ ØªØ±Ø¨Ø· Ø¨ÙŠÙ† Ø£Ù‡Ù… Ø§Ù„ØªØ­Ø¯ÙŠØ«Ø§Øª ÙÙŠ Ù…Ø¬Ø§Ù„ Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©]

## ØªØ·ÙˆØ±Ø§Øª Ù„Ø§ÙØªØ©
[Ù‚Ø§Ø¦Ù…Ø© Ù†Ù‚Ø·ÙŠØ© Ù…Ù† 6â€“8 Ø¹Ù†Ø§ØµØ± Ù…Ø®ØªØµØ±Ø©ØŒ ÙƒÙ„ Ø¹Ù†ØµØ± 1â€“2 Ø¬Ù…Ù„Ø©ØŒ ØªØ´ÙŠØ± Ø¥Ù„Ù‰ Ø´Ø±ÙƒØ§Øª Ø£Ùˆ Ù…Ø¹Ø§ÙŠÙŠØ± Ø£Ùˆ Ù†ØªØ§Ø¦Ø¬ Ù…Ø­Ø¯Ø¯Ø©]

## Ø§Ù„Ø³ÙˆÙ‚ ÙˆØ§Ù„ØªØ£Ø«ÙŠØ±
[1â€“2 ÙÙ‚Ø±Ø© Ø¹Ù† ØªØ£Ø«ÙŠØ± Ø§Ù„Ø£Ø®Ø¨Ø§Ø± Ø¹Ù„Ù‰ Ù‚Ø·Ø§Ø¹ Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© ÙˆØ§Ù„ØµÙ†Ø§Ø¹Ø©]

## Ù…Ø§ Ø§Ù„Ø°ÙŠ Ù†ØªØ±Ù‚Ø¨Ù‡ Ù„Ø§Ø­Ù‚Ù‹Ø§
[3â€“5 Ù†Ù‚Ø§Ø· Ø­ÙˆÙ„ Ø§Ù„Ø¥Ø¹Ù„Ø§Ù†Ø§Øª Ø§Ù„Ù…ØªÙˆÙ‚Ø¹Ø© Ø£Ùˆ Ø§Ù„Ø§ØªØ¬Ø§Ù‡Ø§Øª ÙÙŠ Ù…Ø¬Ø§Ù„ Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø§Ù„ØµØ§Ø¹Ø¯Ø©]

Ù…ØªØ·Ù„Ø¨Ø§Øª Ø£Ø³Ø§Ø³ÙŠØ©:
- Ø§Ø³ØªØ®Ø¯Ù… Ø¹Ù†Ø§ÙˆÙŠÙ† Ø§Ù„Ø£Ù‚Ø³Ø§Ù… Ø§Ù„Ø¹Ø±Ø¨ÙŠØ© Ø£Ø¹Ù„Ø§Ù‡ ÙƒÙ…Ø§ Ù‡ÙŠ Ù…Ø¹ ØªÙ†Ø³ÙŠÙ‚ Markdown (##).
- Ø§Ù…Ø²Ø¬ Ø§Ù„Ù…Ø¹Ù„ÙˆÙ…Ø§Øª Ù…Ù† Ø¹Ø¯Ø© Ù…Ù‚Ø§Ù„Ø§ØªØŒ ÙˆÙ„Ø§ ØªÙƒØªÙÙ Ø¨Ø³Ø±Ø¯Ù‡Ø§ ÙˆØ§Ø­Ø¯Ø© ØªÙ„Ùˆ Ø§Ù„Ø£Ø®Ø±Ù‰.
- Ø§Ø°ÙƒØ± Ø§Ù„Ø£Ø³Ù…Ø§Ø¡ ÙˆØ§Ù„Ø£Ø±Ù‚Ø§Ù… ÙˆØ§Ù„Ù…Ø¹Ø§ÙŠÙŠØ± ÙˆØ§Ù„Ù…Ù†Ø¸Ù…Ø§Øª ÙƒÙ„Ù…Ø§ Ø£Ù…ÙƒÙ† Ø°Ù„Ùƒ.
- Ø§Ø¬Ø¹Ù„ Ø§Ù„Ø£Ø³Ù„ÙˆØ¨ ØµØ­ÙÙŠÙ‹Ø§ Ø§Ø­ØªØ±Ø§ÙÙŠÙ‹Ø§ ÙˆÙˆØ§Ø¶Ø­Ù‹Ø§ØŒ Ù…Ù†Ø§Ø³Ø¨Ù‹Ø§ Ù„ØªÙ‚Ø±ÙŠØ± ÙŠÙˆÙ…ÙŠ Ø¹Ù† Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©.
- Ø±ÙƒÙ‘Ø² Ø¯Ø§Ø¦Ù…Ù‹Ø§ Ø¹Ù„Ù‰ ØµÙ„Ø© Ø§Ù„Ù…Ø­ØªÙˆÙ‰ Ø¨Ù…Ø¬Ø§Ù„ Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©.

Ù…Ù‚Ø§Ù„Ø§Øª Ù„Ù„ØªØ­Ù„ÙŠÙ„ ({max_daily_articles} Ù…Ù‚Ø§Ù„Ø§Ù‹):
{news_content}
"""
    
    # Call AWS Bedrock Claude API
    content, error = call_claude_api(
        system_message=system_message,
        user_message=user_prompt,
        max_tokens=2200,
        temperature=0.45,
        use_cache=True
    )
    
    if error:
        return f"# Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„ÙŠÙˆÙ…ÙŠ Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©{title_suffix}\n\nØ­Ø¯Ø« Ø®Ø·Ø£ Ø£Ø«Ù†Ø§Ø¡ ØªÙˆÙ„ÙŠØ¯ Ø§Ù„Ù…Ø­ØªÙˆÙ‰: {error}"
    
    if not content:
        return f"# Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„ÙŠÙˆÙ…ÙŠ Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©{title_suffix}\n\nØªØ¹Ø°Ù‘Ø± ØªÙˆÙ„ÙŠØ¯ Ø§Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„ÙŠÙˆÙ…."
    
    if not content.lstrip().startswith('#'):
        prefix_title = f"# Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„ÙŠÙˆÙ…ÙŠ Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©{title_suffix}\n\n"
        return prefix_title + content
    
    logger.info(f"Model content length: {len(content)}")
    return content

def build_fallback_hajj_blog_content(articles, category=None):
    """Build a minimal, readable daily report from available articles when the model response is empty."""
    heading = f"# Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„ÙŠÙˆÙ…ÙŠ Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© â€“ {category}" if category else "# Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„ÙŠÙˆÙ…ÙŠ Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©"
    if not articles:
        return f"{heading}\n\nÙ„Ø§ ØªÙˆØ¬Ø¯ Ù…Ù‚Ø§Ù„Ø§Øª Ù…ØªØ§Ø­Ø© Ø§Ù„ÙŠÙˆÙ…."
    lines = [heading, "", "## Ø£Ù‡Ù… Ø§Ù„Ø¹Ù†Ø§ÙˆÙŠÙ†", ""]
    count = 0
    for art in articles:
        if not art:
            continue
        title = art.get('title') or art.get('headline') or art.get('name')
        desc = art.get('description') or art.get('summary') or art.get('excerpt') or art.get('full_content', '')[:200]
        if not title and not desc:
            continue
        bullet = f"- {title.strip()}" if title else "- (Ø¨Ø¯ÙˆÙ† Ø¹Ù†ÙˆØ§Ù†)"
        if desc:
            bullet += f" â€” {desc.strip()[:240]}"
        lines.append(bullet)
        count += 1
        if count >= 20:
            break
    if count == 0:
        lines.append("- Ù„Ø§ ØªÙˆØ¬Ø¯ Ø¹Ù†Ø§ØµØ± Ù‚Ø§Ø¨Ù„Ø© Ù„Ù„Ø¹Ø±Ø¶.")
    lines += ["", "## Ù…Ù„Ø§Ø­Ø¸Ø§Øª", "ØªÙ… Ø¥Ù†Ø´Ø§Ø¡ Ù‡Ø°Ø§ Ø§Ù„Ù…Ù„Ø®Øµ Ø§Ù„Ø§Ø­ØªÙŠØ§Ø·ÙŠ Ø¨Ø³Ø¨Ø¨ Ø¹Ø¯Ù… ØªÙˆÙØ± Ø§Ø³ØªØ¬Ø§Ø¨Ø© Ù…Ù† Ù†Ù…ÙˆØ°Ø¬ Ø§Ù„Ø°ÙƒØ§Ø¡ Ø§Ù„Ø§ØµØ·Ù†Ø§Ø¹ÙŠ."]
    return "\n".join(lines)

def generate_hajj_blog_with_ai(articles, blog_theme, time_period="weekly", keywords=None):
    """Generate a Hajj and Umrah blog post using Claude AI"""
    
    if not articles:
        logger.warning(f"No articles provided to generate_hajj_blog_with_ai for {blog_theme}")
        return "ØªØ¹Ø°Ù‘Ø± Ø¥Ù†Ø´Ø§Ø¡ Ù…Ø¯ÙˆÙ†Ø© Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©: Ù„Ø§ ØªÙˆØ¬Ø¯ Ù…Ù‚Ø§Ù„Ø§Øª ÙƒØ§ÙÙŠØ© Ù„Ù„ØªØ­Ù„ÙŠÙ„."
    
    # Prepare content from articles
    news_content = ""
    article_count = min(len(articles), 30)
    
    for i, article in enumerate(articles[:article_count], 1):
        if not article:
            continue
        title = article.get('title', 'No title')
        source = article.get('source', {}).get('name', 'Unknown source') if article.get('source') else 'Unknown source'
        full_content = article.get('full_content', article.get('description', 'No content'))
        published_date = article.get('publishedAt', 'Unknown date')
        url = article.get('url', '')
        
        if full_content and len(full_content) > 600:
            full_content = full_content[:600] + "..."
        
        news_content += f"""
ARTICLE {i}:
Title: {title}
Source: {source}
Date: {published_date}
URL: {url}
Content: {full_content or 'No content available'}
---
"""
    
    # Determine period-specific language (Arabic labels)
    period_adj = "Ø£Ø³Ø¨ÙˆØ¹ÙŠØ©" if time_period == "weekly" else "Ø´Ù‡Ø±ÙŠØ©"
    period_cap = "Ù‡Ø°Ø§ Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹" if time_period == "weekly" else "Ù‡Ø°Ø§ Ø§Ù„Ø´Ù‡Ø±"
    period_next = "Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹ Ø§Ù„Ù‚Ø§Ø¯Ù…" if time_period == "weekly" else "Ø§Ù„Ø´Ù‡Ø± Ø§Ù„Ù‚Ø§Ø¯Ù…"
    
    # Create theme-specific prompts
    if blog_theme == "management":
        blog_focus = "Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬Ø§Ø¬ ÙˆØ§Ù„ØªÙ†Ø¸ÙŠÙ…"
        blog_angle = (
            "Ø±ÙƒÙ‘Ø² Ø¹Ù„Ù‰ ØªØ·ÙˆØ±Ø§Øª Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬Ø§Ø¬ØŒ ÙˆØ§Ù„ØªÙ†Ø¸ÙŠÙ…ØŒ ÙˆØ§Ù„Ø§Ø³ØªØ¹Ø¯Ø§Ø¯Ø§ØªØŒ "
            "ÙˆØ§Ù„ØªØ´Ø±ÙŠØ¹Ø§Øª ÙˆØ§Ù„Ø³ÙŠØ§Ø³Ø§Øª Ø§Ù„Ù…ØªØ¹Ù„Ù‚Ø© Ø¨Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©ØŒ ÙˆØ§Ù„ØªØ·ÙˆØ±Ø§Øª ÙÙŠ Ù‚Ø·Ø§Ø¹ Ø§Ù„Ø­Ø¬. "
            "Ø§Ù„Ø¬Ù…Ù‡ÙˆØ± Ø§Ù„Ù…Ø³ØªÙ‡Ø¯Ù Ù‡Ùˆ Ø§Ù„Ù…Ø³Ø¤ÙˆÙ„ÙˆÙ† Ø¹Ù† Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©ØŒ ÙˆØ§Ù„Ù…Ù†Ø¸Ù…ÙˆÙ†ØŒ ÙˆØ§Ù„Ù…Ù‡ØªÙ…ÙˆÙ† Ø¨Ø§Ù„Ù‚Ø·Ø§Ø¹. "
            "Ø£Ø¨Ø±Ø² Ø§Ø³ØªØ±Ø§ØªÙŠØ¬ÙŠØ§Øª Ø®Ø¯Ù…Ø© Ø§Ù„Ø­Ø¬Ø§Ø¬ØŒ ÙˆØ§Ù„ØªØ·ÙˆÙŠØ±ØŒ ÙˆØ§Ù„Ø´Ø±Ø§ÙƒØ§ØªØŒ ÙˆØ§Ù„Ø§Ø¨ØªÙƒØ§Ø±Ø§Øª ÙÙŠ Ø§Ù„Ù‚Ø·Ø§Ø¹."
        )
    elif blog_theme == "combined":
        blog_focus = "Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø§Ù„Ø´Ø§Ù…Ù„Ø©"
        blog_angle = (
            "Ø±ÙƒÙ‘Ø² Ø¹Ù„Ù‰ ÙƒØ§ÙØ© Ø¬ÙˆØ§Ù†Ø¨ Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø¨Ù…Ø§ ÙÙŠ Ø°Ù„Ùƒ Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬Ø§Ø¬ØŒ Ø§Ù„ØªÙ†Ø¸ÙŠÙ… Ø§Ù„Ø¥Ø¯Ø§Ø±ÙŠØŒ Ø§Ù„ØªÙ‚Ù†ÙŠØ© ÙˆØ§Ù„Ø§Ø¨ØªÙƒØ§Ø±ØŒ "
            "ÙˆØ§Ù„ØªØ·ÙˆØ±Ø§Øª Ø§Ù„ØµØ­ÙŠØ© ÙˆØ§Ù„Ø£Ù…Ù†ÙŠØ©. "
            "Ø§Ù„Ø¬Ù…Ù‡ÙˆØ± Ø§Ù„Ù…Ø³ØªÙ‡Ø¯Ù Ù‡Ùˆ Ø§Ù„Ù…ØªØ§Ø¨Ø¹ÙˆÙ† Ø§Ù„Ø´Ø§Ù…Ù„ÙˆÙ† Ù„Ù‚Ø·Ø§Ø¹ Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© ÙˆØ§Ù„Ù…Ù‡ØªÙ…ÙˆÙ† Ø¨Ø¬Ù…ÙŠØ¹ Ù…Ø³ØªØ¬Ø¯Ø§ØªÙ‡. "
            "Ø£Ø¨Ø±Ø² Ø£Ù‡Ù… Ø§Ù„Ø£Ø®Ø¨Ø§Ø± ÙˆØ§Ù„Ù‚Ø±Ø§Ø±Ø§Øª ÙˆØ§Ù„ØªØ·ÙˆØ±Ø§Øª Ø§Ù„ØªÙƒÙ†ÙˆÙ„ÙˆØ¬ÙŠØ© ÙˆØ§Ù„ØªÙ†Ø¸ÙŠÙ…ÙŠØ© ÙÙŠ Ø§Ù„Ù‚Ø·Ø§Ø¹."
        )
    else:  # improvement
        blog_focus = "Ø§Ù„ØªÙ‚Ù†ÙŠØ© ÙˆØ§Ù„ØµØ­Ø© ÙˆØ§Ù„Ø§Ø¨ØªÙƒØ§Ø± ÙÙŠ Ø§Ù„Ø­Ø¬"
        blog_angle = (
            "Ø±ÙƒÙ‘Ø² Ø¹Ù„Ù‰ Ø§Ù„ØªÙ‚Ù†ÙŠØ© ÙˆØ§Ù„Ø§Ø¨ØªÙƒØ§Ø± ÙˆØ§Ù„ØªØ­ÙˆÙ„ Ø§Ù„Ø±Ù‚Ù…ÙŠ ÙÙŠ Ø®Ø¯Ù…Ø© Ø§Ù„Ø­Ø¬Ø§Ø¬ ÙˆØ§Ù„Ù…Ø¹ØªÙ…Ø±ÙŠÙ†ØŒ "
            "ÙˆØ§Ù„Ø®Ø¯Ù…Ø§Øª Ø§Ù„ØµØ­ÙŠØ© ÙˆØ§Ù„Ø³Ù„Ø§Ù…Ø© ÙˆØ§Ù„Ø£Ù…Ù† Ø®Ù„Ø§Ù„ Ù…ÙˆØ³Ù… Ø§Ù„Ø­Ø¬ØŒ ÙˆØ§Ù„Ù…Ø¨Ø§Ø¯Ø±Ø§Øª Ø§Ù„ØªØ·ÙˆÙŠØ±ÙŠØ©. "
            "Ø§Ù„Ø¬Ù…Ù‡ÙˆØ± Ø§Ù„Ù…Ø³ØªÙ‡Ø¯Ù Ù‡Ùˆ Ø§Ù„Ù…Ù‡ØªÙ…ÙˆÙ† Ø¨ØªØ·ÙˆÙŠØ± Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬ØŒ ÙˆØ§Ù„Ù…Ù†Ø¸Ù‘Ù…ÙˆÙ†ØŒ ÙˆÙ…Ø²ÙˆØ¯Ùˆ Ø§Ù„Ø®Ø¯Ù…Ø§Øª. "
            "Ø£Ø¨Ø±Ø² Ø§Ù„Ø§ØªØ¬Ø§Ù‡Ø§Øª Ø§Ù„ØµØ§Ø¹Ø¯Ø©ØŒ ÙˆØ§Ù„Ø§Ø¨ØªÙƒØ§Ø±Ø§ØªØŒ ÙˆØ£ÙØ¶Ù„ Ø§Ù„Ù…Ù…Ø§Ø±Ø³Ø§Øª ÙÙŠ Ø®Ø¯Ù…Ø© Ø¶ÙŠÙˆÙ Ø§Ù„Ø±Ø­Ù…Ù†."
        )
    
    system_message = (
        "You are a professional Arabic Hajj and Umrah industry blogger. "
        "You always write engaging, insightful blog posts in MODERN STANDARD ARABIC about Hajj, Umrah, and pilgrimage developments. "
        "Use clear structure, strong headings in Arabic, and actionable insights. "
        "Always use proper markdown formatting for headers, and keep the tone ØµØ­ÙÙŠ Ø§Ø­ØªØ±Ø§ÙÙŠ ÙˆØ¬Ø°Ù‘Ø§Ø¨."
    )
    
    keyword_guidance = build_keyword_instruction_block(keywords)
    
    user_prompt = f"""
    {keyword_guidance}

    Ø§ÙƒØªØ¨ ØªØ¯ÙˆÙŠÙ†Ø© {period_adj} Ø¹Ø±Ø¨ÙŠØ© Ø´Ø§Ù…Ù„Ø© Ø¹Ù† {blog_focus} Ø®Ù„Ø§Ù„ {period_cap}ØŒ
    Ù…Ø³ØªØ®Ø¯Ù…Ù‹Ø§ Ø§Ù„Ø¨Ù†ÙŠØ© Ø§Ù„ØªØ§Ù„ÙŠØ© **Ø¨Ø§Ù„Ø¶Ø¨Ø·** Ø¨Ø§Ø³ØªØ®Ø¯Ø§Ù… Markdown:

    # [Ø§ÙƒØªØ¨ Ø¹Ù†ÙˆØ§Ù†Ù‹Ø§ Ø¹Ø±Ø¨ÙŠÙ‹Ø§ Ø¬Ø°Ø§Ø¨Ù‹Ø§]

    ## Ù…Ù‚Ø¯Ù…Ø©
    [Ù…Ù‚Ø¯Ù…Ø© Ù…Ø´ÙˆÙ‘Ù‚Ø© Ù…Ù† 150 ÙƒÙ„Ù…Ø© ØªÙ‚Ø±ÙŠØ¨Ù‹Ø§ ØªØ¬Ø°Ø¨ Ø§Ù„Ù‚Ø§Ø±Ø¦ ÙˆØªØ´Ø±Ø­ Ø³ÙŠØ§Ù‚ Ø§Ù„ØªÙ‚Ø±ÙŠØ±]

    ## Ø£Ù‡Ù… Ù‚ØµØ© ÙÙŠ {period_cap}
    [250â€“300 ÙƒÙ„Ù…Ø© ØªØºØ·ÙŠ Ø§Ù„ØªØ·ÙˆØ± Ø§Ù„Ø£Ù‡Ù… ÙÙŠ Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ù„Ù‡Ø°Ø§ {period_cap}]

    ## ØªØ·ÙˆØ± Ø±Ø¦ÙŠØ³ÙŠ Ø«Ø§Ù†Ù
    [250â€“300 ÙƒÙ„Ù…Ø© Ø¹Ù† Ø«Ø§Ù†ÙŠ Ø£Ù‡Ù… ØªØ·ÙˆØ±]

    ## Ø§ØªØ¬Ø§Ù‡Ø§Øª Ø¨Ø§Ø±Ø²Ø©
    [200â€“250 ÙƒÙ„Ù…Ø© Ø¹Ù† Ø£Ø¨Ø±Ø² Ø§Ù„Ø§ØªØ¬Ø§Ù‡Ø§Øª ÙˆØ§Ù„Ø£Ù†Ù…Ø§Ø· Ø§Ù„Ù…Ù„Ø­ÙˆØ¸Ø©]

    ## ØªØ±ÙƒÙŠØ² Ø¹Ù„Ù‰ Ù…Ø¹ÙŠØ§Ø± Ø£Ùˆ Ù‚Ø·Ø§Ø¹
    [200â€“250 ÙƒÙ„Ù…Ø© ØªØ¨Ø±Ø² Ù…Ø¹Ø§ÙŠÙŠØ± Ø£Ùˆ Ø´Ø±ÙƒØ§Øª Ø£Ùˆ Ù‚Ø·Ø§Ø¹Ø§Øª Ù…Ø­Ø¯Ø¯Ø©]

    ## Ù…Ù„Ø®ØµØ§Øª Ø³Ø±ÙŠØ¹Ø©
    [200â€“250 ÙƒÙ„Ù…Ø© ØªØºØ·ÙŠ 6â€“8 ØªØ·ÙˆØ±Ø§Øª Ø¥Ø¶Ø§ÙÙŠØ© Ø¨Ø´ÙƒÙ„ Ù…ÙˆØ¬Ø²]

    ## Ù…Ø±Ø§Ù‚Ø¨Ø© Ø§Ù„Ø³ÙˆÙ‚
    [150â€“200 ÙƒÙ„Ù…Ø© Ø¹Ù† Ø§Ù„Ø§Ø³ØªØ«Ù…Ø§Ø±Ø§ØªØŒ Ø§Ù„Ø´Ø±Ø§ÙƒØ§ØªØŒ ÙˆØ£Ø®Ø¨Ø§Ø± Ø§Ù„Ø£Ø¹Ù…Ø§Ù„ ÙÙŠ Ù…Ø¬Ø§Ù„ Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©]

    ## Ù…Ø§ Ø§Ù„Ø°ÙŠ ÙŠÙ†ØªØ¸Ø±Ù†Ø§ Ù„Ø§Ø­Ù‚Ù‹Ø§
    [100â€“150 ÙƒÙ„Ù…Ø© ØªØ³ØªØ´Ø±Ù Ù…Ø§ Ù‚Ø¯ ÙŠØ­Ø¯Ø« ÙÙŠ {period_next}]

    ## Ø®Ù„Ø§ØµØ©
    [ÙÙ‚Ø±Ø© Ø®ØªØ§Ù…ÙŠØ© Ù‚ØµÙŠØ±Ø© Ø¨Ø£Ù‡Ù… Ø§Ù„Ø±Ø³Ø§Ø¦Ù„ ÙˆØ§Ù„ØªÙˆØµÙŠØ§Øª]

    Ø²Ø§ÙˆÙŠØ© Ø§Ù„ØªØºØ·ÙŠØ©:
    {blog_angle}

    Ù…ØªØ·Ù„Ø¨Ø§Øª Ø£Ø³Ø§Ø³ÙŠØ©:
    - ÙŠØ¬Ø¨ Ø§Ø³ØªØ®Ø¯Ø§Ù… Ø¹Ù†Ø§ÙˆÙŠÙ† Ø§Ù„Ø£Ù‚Ø³Ø§Ù… Ø§Ù„Ø¹Ø±Ø¨ÙŠØ© Ø£Ø¹Ù„Ø§Ù‡ ÙƒÙ…Ø§ Ù‡ÙŠ Ù…Ø¹ ØªÙ†Ø³ÙŠÙ‚ Markdown (##).
    - Ø§Ø³ØªØ´Ù‡Ø¯ Ø¨Ù…Ø§ Ù„Ø§ ÙŠÙ‚Ù„ Ø¹Ù† 15â€“20 Ù…Ù‚Ø§Ù„Ù‹Ø§ Ù…Ø®ØªÙ„ÙÙ‹Ø§ Ø¯Ø§Ø®Ù„ Ø§Ù„ØªØ¯ÙˆÙŠÙ†Ø©.
    - Ø§Ø°ÙƒØ± Ø£Ø³Ù…Ø§Ø¡ Ø§Ù„Ø´Ø±ÙƒØ§ØªØŒ Ø§Ù„Ù…Ø¹Ø§ÙŠÙŠØ±ØŒ Ø§Ù„Ø£Ø±Ù‚Ø§Ù…ØŒ Ø§Ù„ØªÙˆØ§Ø±ÙŠØ®ØŒ ÙˆØ§Ù„Ù…ØµØ§Ø¯Ø± ÙƒÙ„Ù…Ø§ Ø£Ù…ÙƒÙ†.
    - Ø§Ø¬Ø¹Ù„ Ø§Ù„Ø£Ø³Ù„ÙˆØ¨ Ø¹Ø±Ø¨ÙŠÙ‹Ø§ ØµØ­ÙÙŠÙ‹Ø§ Ù…Ù‡Ù†ÙŠÙ‹Ø§ ÙˆØ¬Ø°Ø§Ø¨Ù‹Ø§.
    - Ø§Ø¬Ø¹Ù„ ÙƒÙ„ Ù‚Ø³Ù… ØºÙ†ÙŠÙ‹Ø§ Ø¨Ø§Ù„Ù…Ø¹Ù„ÙˆÙ…Ø§Øª ÙˆÙ‚Ø§Ø¨Ù„Ù‹Ø§ Ù„Ù„Ø§Ø³ØªØ®Ø¯Ø§Ù… Ù„Ø®Ø¨Ø±Ø§Ø¡ Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©.
    - Ø£Ù…Ø§Ù…Ùƒ {article_count} Ù…Ù‚Ø§Ù„Ù‹Ø§ØŒ ÙØ§Ø³ØªØ®Ø¯Ù… Ù‡Ø°Ø§ Ø§Ù„ØªÙ†ÙˆØ¹ ÙÙŠ Ø¨Ù†Ø§Ø¡ Ø§Ù„ØµÙˆØ±Ø© Ø§Ù„ÙƒÙ„ÙŠØ©.

    Ù…Ø­ØªÙˆÙ‰ Ø§Ù„Ù…Ù‚Ø§Ù„Ø§Øª Ù„Ù„ØªØ­Ù„ÙŠÙ„ ({article_count} Ù…Ù‚Ø§Ù„Ø§Ù‹):
    {news_content}

    Ø§ÙƒØªØ¨ Ø§Ù„ØªØ¯ÙˆÙŠÙ†Ø© Ø¨Ø§Ù„Ù„ØºØ© Ø§Ù„Ø¹Ø±Ø¨ÙŠØ© Ø§Ù„ÙØµØ­Ù‰ ÙÙ‚Ø·ØŒ Ø¨Ø¯ÙˆÙ† Ø£ÙŠ ÙÙ‚Ø±Ø§Øª ØªÙØ³ÙŠØ±ÙŠØ© Ø¨Ø§Ù„Ù„ØºØ© Ø§Ù„Ø¥Ù†Ø¬Ù„ÙŠØ²ÙŠØ©.
    """
    
    content, error = call_claude_api(
        system_message=system_message,
        user_message=user_prompt,
        max_tokens=3500,
        temperature=0.5,
        use_cache=True
    )
    
    if error:
        return f"Ø­Ø¯Ø« Ø®Ø·Ø£ Ø£Ø«Ù†Ø§Ø¡ ØªÙˆÙ„ÙŠØ¯ Ø§Ù„ØªØ¯ÙˆÙŠÙ†Ø©: {error}"
    else:
        return content

async def generate_weekly_blogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate comprehensive weekly Hajj and Umrah blog posts."""
    user_id = get_user_id(update)
    
    # Check usage limit
    has_limit, current_usage = check_usage_limit(user_id, 'weekly')
    if not has_limit:
        limit_message = (
            f"âŒ *ØªÙ… Ø§Ù„ÙˆØµÙˆÙ„ Ø¥Ù„Ù‰ Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ù‚ØµÙ‰*\n\n"
            f"Ù„Ù‚Ø¯ Ø§Ø³ØªØ®Ø¯Ù…Øª Ø¬Ù…ÙŠØ¹ Ø§Ù„Ù…Ø­Ø§ÙˆÙ„Ø§Øª Ø§Ù„Ù…ØªØ§Ø­Ø© Ù„Ù„ØªÙ‚Ø§Ø±ÙŠØ± Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹ÙŠØ© ({USAGE_LIMITS['weekly']}/{USAGE_LIMITS['weekly']}).\n\n"
        )
        if update.callback_query:
            await update.callback_query.answer("ØªÙ… Ø§Ù„ÙˆØµÙˆÙ„ Ø¥Ù„Ù‰ Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ù‚ØµÙ‰", show_alert=True)
            await update.callback_query.message.reply_text(limit_message, parse_mode='Markdown')
        else:
            await update.message.reply_text(limit_message, parse_mode='Markdown')
        return
    
    # Increment usage
    increment_usage(user_id, 'weekly')
    
    if update.callback_query:
        await update.callback_query.answer()
        message = await update.callback_query.message.reply_text(
            "ðŸ“ *Ù…ÙˆÙ„Ù‘Ø¯ Ø§Ù„Ù…Ø¯ÙˆÙ†Ø§Øª Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹ÙŠØ© Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©*\n\nâ³ Ø¬Ø§Ø±Ù Ø¥Ø¹Ø¯Ø§Ø¯ ØªØ­Ù„ÙŠÙ„ Ø£Ø³Ø¨ÙˆØ¹ÙŠ Ø´Ø§Ù…Ù„...\nðŸ“Š Ø³ÙŠØªÙ… ØªØ­Ù„ÙŠÙ„ Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ù„Ø¢Ø®Ø± 7 Ø£ÙŠØ§Ù…\nâ° Ø§Ù„Ø²Ù…Ù† Ø§Ù„Ù…ØªÙˆÙ‚Ø¹: 3â€“5 Ø¯Ù‚Ø§Ø¦Ù‚\n\nÙŠØ±Ø¬Ù‰ Ø§Ù„Ø§Ù†ØªØ¸Ø§Ø±...",
            parse_mode='Markdown'
        )
    else:
        message = await update.message.reply_text(
            "ðŸ“ *Ù…ÙˆÙ„Ù‘Ø¯ Ø§Ù„Ù…Ø¯ÙˆÙ†Ø§Øª Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹ÙŠØ© Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©*\n\nâ³ Ø¬Ø§Ø±Ù Ø¥Ø¹Ø¯Ø§Ø¯ ØªØ­Ù„ÙŠÙ„ Ø£Ø³Ø¨ÙˆØ¹ÙŠ Ø´Ø§Ù…Ù„...\nðŸ“Š Ø³ÙŠØªÙ… ØªØ­Ù„ÙŠÙ„ Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ù„Ø¢Ø®Ø± 7 Ø£ÙŠØ§Ù…\nâ° Ø§Ù„Ø²Ù…Ù† Ø§Ù„Ù…ØªÙˆÙ‚Ø¹: 3â€“5 Ø¯Ù‚Ø§Ø¦Ù‚\n\nÙŠØ±Ø¬Ù‰ Ø§Ù„Ø§Ù†ØªØ¸Ø§Ø±...",
            parse_mode='Markdown'
        )
    
    try:
        await message.edit_text(
            "ðŸ“ *Ø§Ù„Ø®Ø·ÙˆØ© 1/4:* Ø¬Ù„Ø¨ Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹ÙŠØ©...\nðŸ“¡ ÙŠØªÙ… Ø§Ù„Ø¢Ù† Ø¬Ù…Ø¹ Ø§Ù„Ù…Ù‚Ø§Ù„Ø§Øª Ù…Ù† Ø¢Ø®Ø± 7 Ø£ÙŠØ§Ù…...",
            parse_mode='Markdown'
        )
        
        hajgov_articles = fetch_hajgov_news() or []
        cnn_articles = fetch_cnn_hajj_news() or []

        logger.info(f"Fetched {len(hajgov_articles)} haj.gov.sa, {len(cnn_articles)} CNN Arabic")
        
        await message.edit_text(
            "ðŸ“ *Ø§Ù„Ø®Ø·ÙˆØ© 2/4:* ØªØµÙÙŠØ© Ø§Ù„Ù…Ù‚Ø§Ù„Ø§Øª...\nðŸ” ØªØµÙÙŠØ© Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©...",
            parse_mode='Markdown'
        )
        
        # No filtering needed - sources are already Hajj-specific

        recent_hajgov = filter_recent_articles(hajgov_articles, days=7) or []
        recent_cnn = filter_recent_articles(cnn_articles, days=7) or []

        all_articles = recent_hajgov + recent_cnn
        logger.info(f"Total relevant articles: {len(all_articles)}")
        
        if not all_articles:
            await message.edit_text(
                "âŒ Ù„Ù… ÙŠØªÙ… Ø§Ù„Ø¹Ø«ÙˆØ± Ø¹Ù„Ù‰ Ø£Ø®Ø¨Ø§Ø± Ø­Ø¬ ÙˆØ¹Ù…Ø±Ø© ÙƒØ§ÙÙŠØ©. ÙŠØ±Ø¬Ù‰ Ø§Ù„Ù…Ø­Ø§ÙˆÙ„Ø© Ù„Ø§Ø­Ù‚Ù‹Ø§.",
                parse_mode='Markdown'
            )
            return
        
        await message.edit_text(
            f"ðŸ“ *Ø§Ù„Ø®Ø·ÙˆØ© 3/4:* Ø§Ø³ØªØ®Ø±Ø§Ø¬ Ø§Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„ÙƒØ§Ù…Ù„...\nðŸ“– Ø¬Ø§Ø±ÙŠ Ù…Ø¹Ø§Ù„Ø¬Ø© {min(len(all_articles), 50)} Ù…Ù‚Ø§Ù„Ø§Øª ØªÙ‚Ø±ÙŠØ¨Ù‹Ø§\nâ±ï¸ Ù‚Ø¯ ÙŠØ³ØªØºØ±Ù‚ Ù‡Ø°Ø§ Ù…Ù† 2â€“3 Ø¯Ù‚Ø§Ø¦Ù‚...",
            parse_mode='Markdown'
        )
        
        enhanced_articles = enhance_articles_with_content(all_articles, max_articles=50, weekly_mode=True) or []
        enhanced_count = len([a for a in enhanced_articles if a.get('full_content')])
        logger.info(f"Enhanced articles: {enhanced_count}/{len(enhanced_articles)}")
        
        await message.edit_text(
            "ðŸ“ *Ø§Ù„Ø®Ø·ÙˆØ© 4/6:* ØªÙˆÙ„ÙŠØ¯ ØªÙ‚Ø±ÙŠØ± Ø£Ø³Ø¨ÙˆØ¹ÙŠ Ø¨Ø§Ø³ØªØ®Ø¯Ø§Ù… Ø§Ù„Ø°ÙƒØ§Ø¡ Ø§Ù„Ø§ØµØ·Ù†Ø§Ø¹ÙŠ...\nâœï¸ ÙŠØªÙ… Ø§Ù„Ø¢Ù† Ø¥Ù†Ø´Ø§Ø¡ ØªØ­Ù„ÙŠÙ„ Ø£Ø³Ø¨ÙˆØ¹ÙŠ Ø´Ø§Ù…Ù„...",
            parse_mode='Markdown'
        )
        
        user_keywords = get_user_keywords(context)
        
        logger.info(f"Total blog articles for combined report: {len(enhanced_articles)}")
        
        # Generate Combined Blog
        combined_blog = None
        if enhanced_articles:
            combined_blog = generate_hajj_blog_with_ai(
                enhanced_articles, "combined", "weekly", keywords=user_keywords
            )
        
        # Step 5: Create PDFs
        await message.edit_text(
            "ðŸ“ *Ø§Ù„Ø®Ø·ÙˆØ© 5/6:* Ø¥Ù†Ø´Ø§Ø¡ Ù…Ù„ÙØ§Øª PDF Ø§Ø­ØªØ±Ø§ÙÙŠØ©...\nðŸ“„ ÙŠØªÙ… Ø§Ù„Ø¢Ù† ØªÙ†Ø³ÙŠÙ‚ Ø§Ù„ØªÙ‚Ø±ÙŠØ±...",
            parse_mode='Markdown'
        )
        
        combined_filename = None
        
        if combined_blog:
            combined_filename = create_hajj_blog_pdf(
                combined_blog,
                "Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹ÙŠ Ø§Ù„Ø´Ø§Ù…Ù„ Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©",
                is_temp_file=True
            )
        
        # Step 6: Send the blog PDFs
        await message.edit_text(
            "ðŸ“ *Ø§Ù„Ø®Ø·ÙˆØ© 6/6:* Ø¥Ø±Ø³Ø§Ù„ Ù…Ù„ÙØ§Øª PDF...\nðŸ“¤ ÙŠØªÙ… Ø§Ù„Ø¢Ù† Ø¥Ø±Ø³Ø§Ù„ Ø§Ù„Ø±Ø¤Ù‰ ÙˆØ§Ù„ØªØ­Ù„ÙŠÙ„Ø§Øª Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹ÙŠØ© Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©...",
            parse_mode='Markdown'
        )
        
        if combined_filename:
            try:
                with open(combined_filename, 'rb') as pdf_file:
                    await message.reply_document(
                        document=pdf_file,
                        filename=f"Hajj_Weekly_Report_{datetime.now().strftime('%Y%m%d')}.pdf",
                        caption="ðŸ“ **Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹ÙŠ Ø§Ù„Ø´Ø§Ù…Ù„ Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©**\nðŸ’¼ ØªØ­Ù„ÙŠÙ„ Ø´Ø§Ù…Ù„ Ù„ÙƒØ§ÙØ© Ø§Ù„ØªØ·ÙˆØ±Ø§Øª ÙˆØ§Ù„Ø£Ø®Ø¨Ø§Ø± ÙÙŠ Ù‚Ø·Ø§Ø¹ Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©",
                        parse_mode='Markdown'
                    )
                os.unlink(combined_filename)
            except Exception as e:
                logger.error(f"Error sending combined PDF: {e}")
        
        # Success message with statistics
        combined_status = "Generated" if combined_blog else "Skipped (insufficient data)"
        
        success_message = f"""
 âœ… **ØªÙ… Ø§Ù„Ø§Ù†ØªÙ‡Ø§Ø¡ Ù…Ù† ØªÙˆÙ„ÙŠØ¯ Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹ÙŠ Ø§Ù„Ø´Ø§Ù…Ù„ Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø¨Ù†Ø¬Ø§Ø­!**

 ðŸ“Š **Ø¥Ø­ØµØ§Ø¦ÙŠØ§Øª Ø§Ù„Ù…Ø¹Ø§Ù„Ø¬Ø©:**
 â€¢ Ø¥Ø¬Ù…Ø§Ù„ÙŠ Ø§Ù„Ù…Ù‚Ø§Ù„Ø§Øª Ø§Ù„ØªÙŠ ØªÙ… ØªØ­Ù„ÙŠÙ„Ù‡Ø§: {len(enhanced_articles)}
 â€¢ Ù†Ø¬Ø§Ø­ Ø§Ø³ØªØ®Ø±Ø§Ø¬ Ø§Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„ÙƒØ§Ù…Ù„: {enhanced_count}/{len(enhanced_articles)} ({(enhanced_count/len(enhanced_articles)*100) if enhanced_articles else 0:.1f}%)
 â€¢ Ù†Ø·Ø§Ù‚ Ø§Ù„ØªØºØ·ÙŠØ© Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹ÙŠØ©: {(datetime.now() - timedelta(days=7)).strftime('%B %d')} - {datetime.now().strftime('%B %d, %Y')}

 ðŸ“ **Ø§Ù„ØªÙ‚Ø§Ø±ÙŠØ± Ø§Ù„ØªÙŠ ØªÙ… ØªÙˆÙ„ÙŠØ¯Ù‡Ø§:**
 â€¢ Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹ÙŠ Ø§Ù„Ø´Ø§Ù…Ù„ Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© - {combined_status}

 Ø§Ù„ØªÙ‚Ø±ÙŠØ± ÙŠØ­ØªÙˆÙŠ Ø¹Ù„Ù‰ Ø£Ù‚Ø³Ø§Ù… Ù…Ù†Ø¸Ù…Ø© ÙˆØªØ­Ù„ÙŠÙ„ Ù…ØªØ¹Ù…Ù‚ ÙˆØªÙ†Ø³ÙŠÙ‚ Ø§Ø­ØªØ±Ø§ÙÙŠ!
        """
        
        keyboard = [
            [InlineKeyboardButton("ðŸ”„ ØªÙˆÙ„ÙŠØ¯ ØªÙ‚Ø§Ø±ÙŠØ± Ø£Ø³Ø¨ÙˆØ¹ÙŠØ© Ø¬Ø¯ÙŠØ¯Ø©", callback_data='generate_weekly')],
            [InlineKeyboardButton("ðŸ“° Ø§Ù„Ø£Ø®Ø¨Ø§Ø± Ø§Ù„ÙŠÙˆÙ…ÙŠØ©", callback_data='get_news')],
            [InlineKeyboardButton("ðŸ  Ø§Ù„Ù‚Ø§Ø¦Ù…Ø© Ø§Ù„Ø±Ø¦ÙŠØ³ÙŠØ©", callback_data='main_menu')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await message.edit_text(
            success_message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        
    except Exception as e:
        error_message = f"âŒ Ø­Ø¯Ø« Ø®Ø·Ø£ Ø£Ø«Ù†Ø§Ø¡ ØªÙˆÙ„ÙŠØ¯ Ø§Ù„Ù…Ø¯ÙˆÙ†Ø§Øª Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹ÙŠØ©: {str(e)}"
        logger.error(f"Weekly blog generation error: {str(e)}")
        await message.edit_text(error_message)

async def weekly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /weekly command directly."""
    await generate_weekly_blogs(update, context)

async def generate_monthly_blogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate comprehensive monthly Hajj and Umrah blog posts."""
    user_id = get_user_id(update)
    
    # Check usage limit
    has_limit, current_usage = check_usage_limit(user_id, 'monthly')
    if not has_limit:
        limit_message = (
            f"âŒ *ØªÙ… Ø§Ù„ÙˆØµÙˆÙ„ Ø¥Ù„Ù‰ Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ù‚ØµÙ‰*\n\n"
            f"Ù„Ù‚Ø¯ Ø§Ø³ØªØ®Ø¯Ù…Øª Ø¬Ù…ÙŠØ¹ Ø§Ù„Ù…Ø­Ø§ÙˆÙ„Ø§Øª Ø§Ù„Ù…ØªØ§Ø­Ø© Ù„Ù„ØªÙ‚Ø§Ø±ÙŠØ± Ø§Ù„Ø´Ù‡Ø±ÙŠØ© ({USAGE_LIMITS['monthly']}/{USAGE_LIMITS['monthly']}).\n\n"
        )
        if update.callback_query:
            await update.callback_query.answer("ØªÙ… Ø§Ù„ÙˆØµÙˆÙ„ Ø¥Ù„Ù‰ Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ù‚ØµÙ‰", show_alert=True)
            await update.callback_query.message.reply_text(limit_message, parse_mode='Markdown')
        else:
            await update.message.reply_text(limit_message, parse_mode='Markdown')
        return
    
    # Increment usage
    increment_usage(user_id, 'monthly')
    
    if update.callback_query:
        await update.callback_query.answer()
        message = await update.callback_query.message.reply_text(
            "ðŸ“ *Ù…ÙˆÙ„Ù‘Ø¯ Ø§Ù„Ù…Ø¯ÙˆÙ†Ø§Øª Ø§Ù„Ø´Ù‡Ø±ÙŠØ© Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©*\n\nâ³ Ø¬Ø§Ø±Ù Ø¥Ø¹Ø¯Ø§Ø¯ ØªØ­Ù„ÙŠÙ„ Ø´Ù‡Ø±ÙŠ Ø´Ø§Ù…Ù„...\nðŸ“Š Ø³ÙŠØªÙ… ØªØ­Ù„ÙŠÙ„ Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ù„Ø¢Ø®Ø± 30 ÙŠÙˆÙ…Ù‹Ø§\nâ° Ø§Ù„Ø²Ù…Ù† Ø§Ù„Ù…ØªÙˆÙ‚Ø¹: 5â€“10 Ø¯Ù‚Ø§Ø¦Ù‚\n\nÙŠØ±Ø¬Ù‰ Ø§Ù„Ø§Ù†ØªØ¸Ø§Ø±...",
            parse_mode='Markdown'
        )
    else:
        message = await update.message.reply_text(
            "ðŸ“ *Ù…ÙˆÙ„Ù‘Ø¯ Ø§Ù„Ù…Ø¯ÙˆÙ†Ø§Øª Ø§Ù„Ø´Ù‡Ø±ÙŠØ© Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©*\n\nâ³ Ø¬Ø§Ø±Ù Ø¥Ø¹Ø¯Ø§Ø¯ ØªØ­Ù„ÙŠÙ„ Ø´Ù‡Ø±ÙŠ Ø´Ø§Ù…Ù„...\nðŸ“Š Ø³ÙŠØªÙ… ØªØ­Ù„ÙŠÙ„ Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ù„Ø¢Ø®Ø± Ø³Ù†Ø©\nâ° Ø§Ù„Ø²Ù…Ù† Ø§Ù„Ù…ØªÙˆÙ‚Ø¹: 5â€“10 Ø¯Ù‚Ø§Ø¦Ù‚\n\nÙŠØ±Ø¬Ù‰ Ø§Ù„Ø§Ù†ØªØ¸Ø§Ø±...",
            parse_mode='Markdown'
        )
    
    try:
        await message.edit_text(
            "ðŸ“ *Ø§Ù„Ø®Ø·ÙˆØ© 1/4:* Ø¬Ù„Ø¨ Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø§Ù„Ø´Ù‡Ø±ÙŠØ©...\nðŸ“¡ ÙŠØªÙ… Ø§Ù„Ø¢Ù† Ø¬Ù…Ø¹ Ø§Ù„Ù…Ù‚Ø§Ù„Ø§Øª Ù…Ù† Ø¢Ø®Ø± Ø³Ù†Ø©...",
            parse_mode='Markdown'
        )
        
        hajgov_articles = fetch_hajgov_news() or []
        cnn_articles = fetch_cnn_hajj_news() or []

        logger.info(f"Fetched {len(hajgov_articles)} haj.gov.sa, {len(cnn_articles)} CNN Arabic")
        
        await message.edit_text(
            "ðŸ“ *Ø§Ù„Ø®Ø·ÙˆØ© 2/4:* ØªØµÙÙŠØ© Ø§Ù„Ù…Ù‚Ø§Ù„Ø§Øª...\nðŸ” ØªØµÙÙŠØ© Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©...",
            parse_mode='Markdown'
        )
        
        # No filtering needed - sources are already Hajj-specific

        recent_hajgov = filter_recent_articles(hajgov_articles, days=365) or []
        recent_cnn = filter_recent_articles(cnn_articles, days=365) or []

        all_articles = recent_hajgov + recent_cnn
        logger.info(f"Total relevant articles: {len(all_articles)}")
        
        if not all_articles:
            await message.edit_text(
                "âŒ Ù„Ù… ÙŠØªÙ… Ø§Ù„Ø¹Ø«ÙˆØ± Ø¹Ù„Ù‰ Ø£Ø®Ø¨Ø§Ø± Ø­Ø¬ ÙˆØ¹Ù…Ø±Ø© ÙƒØ§ÙÙŠØ©. ÙŠØ±Ø¬Ù‰ Ø§Ù„Ù…Ø­Ø§ÙˆÙ„Ø© Ù„Ø§Ø­Ù‚Ù‹Ø§.",
                parse_mode='Markdown'
            )
            return
        
        await message.edit_text(
            f"ðŸ“ *Ø§Ù„Ø®Ø·ÙˆØ© 3/4:* Ø§Ø³ØªØ®Ø±Ø§Ø¬ Ø§Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„ÙƒØ§Ù…Ù„...\nðŸ“– Ø¬Ø§Ø±ÙŠ Ù…Ø¹Ø§Ù„Ø¬Ø© {min(len(all_articles), 100)} Ù…Ù‚Ø§Ù„Ø§Øª ØªÙ‚Ø±ÙŠØ¨Ù‹Ø§\nâ±ï¸ Ù‚Ø¯ ÙŠØ³ØªØºØ±Ù‚ Ù‡Ø°Ø§ Ù…Ù† 5â€“8 Ø¯Ù‚Ø§Ø¦Ù‚...",
            parse_mode='Markdown'
        )
        
        enhanced_articles = enhance_articles_with_content(all_articles, max_articles=100, monthly_mode=True) or []
        enhanced_count = len([a for a in enhanced_articles if a.get('full_content')])
        logger.info(f"Enhanced articles: {enhanced_count}/{len(enhanced_articles)}")
        
        await message.edit_text(
            "ðŸ“ *Ø§Ù„Ø®Ø·ÙˆØ© 4/6:* ØªÙˆÙ„ÙŠØ¯ ØªØ¯ÙˆÙŠÙ†Ø§Øª Ø´Ù‡Ø±ÙŠØ© Ø¨Ø§Ø³ØªØ®Ø¯Ø§Ù… Ø§Ù„Ø°ÙƒØ§Ø¡ Ø§Ù„Ø§ØµØ·Ù†Ø§Ø¹ÙŠ...\\nâœï¸ ÙŠØªÙ… Ø§Ù„Ø¢Ù† Ø¥Ù†Ø´Ø§Ø¡ ØªØ­Ù„ÙŠÙ„Ø§Øª Ø´Ù‡Ø±ÙŠØ© Ø´Ø§Ù…Ù„Ø©...",
            parse_mode='Markdown'
        )
        
        user_keywords = get_user_keywords(context)
        categorized = categorize_articles_for_blogs(enhanced_articles)
        management_articles = categorized.get('management', []) or []
        improvement_articles = categorized.get('improvement', []) or []
        
        logger.info(f"Management blog articles: {len(management_articles)}, Improvement blog articles: {len(improvement_articles)}")
        
        # Generate Management Blog
        management_blog = None
        if management_articles:
            management_blog = generate_hajj_blog_with_ai(
                management_articles, "management", "monthly", keywords=user_keywords
            )
        
        # Generate Improvement Blog
        improvement_blog = None
        if improvement_articles:
            improvement_blog = generate_hajj_blog_with_ai(
                improvement_articles, "improvement", "monthly", keywords=user_keywords
            )
        
        # Step 5: Create PDFs
        await message.edit_text(
            "ðŸ“ *Ø§Ù„Ø®Ø·ÙˆØ© 5/6:* Ø¥Ù†Ø´Ø§Ø¡ Ù…Ù„ÙØ§Øª PDF Ø§Ø­ØªØ±Ø§ÙÙŠØ©...\\nðŸ“„ ÙŠØªÙ… Ø§Ù„Ø¢Ù† ØªÙ†Ø³ÙŠÙ‚ Ø§Ù„ØªØ¯ÙˆÙŠÙ†Ø§Øª...",
            parse_mode='Markdown'
        )
        
        management_filename = None
        improvement_filename = None
        
        if management_blog:
            management_filename = create_hajj_blog_pdf(
                management_blog,
                "Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„Ø´Ù‡Ø±ÙŠ Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©",
                is_temp_file=True
            )
        
        if improvement_blog:
            improvement_filename = create_hajj_blog_pdf(
                improvement_blog,
                "Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„Ø´Ù‡Ø±ÙŠ Ù„Ù„ØªÙ‚Ù†ÙŠØ© ÙˆØ§Ù„ØµØ­Ø© ÙˆØ§Ù„Ø§Ø¨ØªÙƒØ§Ø± ÙÙŠ Ø§Ù„Ø­Ø¬",
                is_temp_file=True
            )
        
        #  Step 6: Send the blog PDFs
        await message.edit_text(
            "ðŸ“ *Ø§Ù„Ø®Ø·ÙˆØ© 6/6:* Ø¥Ø±Ø³Ø§Ù„ Ù…Ù„ÙØ§Øª PDF...\\nðŸ“¤ ÙŠØªÙ… Ø§Ù„Ø¢Ù† Ø¥Ø±Ø³Ø§Ù„ Ø§Ù„Ø±Ø¤Ù‰ ÙˆØ§Ù„ØªØ­Ù„ÙŠÙ„Ø§Øª Ø§Ù„Ø´Ù‡Ø±ÙŠØ© Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©...",
            parse_mode='Markdown'
        )
        
        if management_filename:
            try:
                with open(management_filename, 'rb') as pdf_file:
                    await message.reply_document(
                        document=pdf_file,
                        filename=f"Hajj_Management_Monthly_{datetime.now().strftime('%Y%m%d')}.pdf",
                        caption="ðŸ“ **Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„Ø´Ù‡Ø±ÙŠ Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©**\\nðŸ’¼ ØªØ­Ù„ÙŠÙ„ Ø´Ù‡Ø±ÙŠ Ø´Ø§Ù…Ù„ Ù„Ø§ØªØ¬Ø§Ù‡Ø§Øª Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬Ø§Ø¬ ÙˆØªØ·ÙˆØ±Ø§Øª Ø§Ù„ØªÙ†Ø¸ÙŠÙ… ÙˆØ§Ù„Ø¥Ø¯Ø§Ø±Ø©",
                        parse_mode='Markdown'
                    )
                os.unlink(management_filename)
            except Exception as e:
                logger.error(f"Error sending management PDF: {e}")
        
        if improvement_filename:
            try:
                with open(improvement_filename, 'rb') as pdf_file:
                    await message.reply_document(
                        document=pdf_file,
                        filename=f"Hajj_Tech_Innovation_Monthly_{datetime.now().strftime('%Y%m%d')}.pdf",
                        caption="ðŸ“ **Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„Ø´Ù‡Ø±ÙŠ Ù„Ù„ØªÙ‚Ù†ÙŠØ© ÙˆØ§Ù„ØµØ­Ø© ÙˆØ§Ù„Ø§Ø¨ØªÙƒØ§Ø± ÙÙŠ Ø§Ù„Ø­Ø¬**\\nâ­ ØªØ­Ù„ÙŠÙ„ Ø´Ù‡Ø±ÙŠ Ø´Ø§Ù…Ù„ Ù„ØªØ·ÙˆØ±Ø§Øª Ø§Ù„ØªÙ‚Ù†ÙŠØ© ÙˆØ§Ù„Ø§Ø¨ØªÙƒØ§Ø± ÙˆØ®Ø¯Ù…Ø§Øª Ø§Ù„Ø¶ÙŠÙˆÙ",
                        parse_mode='Markdown'
                    )
                os.unlink(improvement_filename)
            except Exception as e:
                logger.error(f"Error sending improvement PDF: {e}")
        
        # Success message with statistics
        management_status = "Generated" if management_blog else "Skipped (insufficient data)"
        improvement_status = "Generated" if improvement_blog else "Skipped (insufficient data)"
        
        success_message = f"""
 âœ… **ØªÙ… Ø§Ù„Ø§Ù†ØªÙ‡Ø§Ø¡ Ù…Ù† ØªÙˆÙ„ÙŠØ¯ Ø§Ù„Ù…Ø¯ÙˆÙ†Ø§Øª Ø§Ù„Ø´Ù‡Ø±ÙŠØ© Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø¨Ù†Ø¬Ø§Ø­!**

 ðŸ“Š **Ø¥Ø­ØµØ§Ø¦ÙŠØ§Øª Ø§Ù„Ù…Ø¹Ø§Ù„Ø¬Ø©:**
 â€¢ Ø¥Ø¬Ù…Ø§Ù„ÙŠ Ø§Ù„Ù…Ù‚Ø§Ù„Ø§Øª Ø§Ù„ØªÙŠ ØªÙ… ØªØ­Ù„ÙŠÙ„Ù‡Ø§: {len(enhanced_articles)}
 â€¢ Ù†Ø¬Ø§Ø­ Ø§Ø³ØªØ®Ø±Ø§Ø¬ Ø§Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„ÙƒØ§Ù…Ù„: {enhanced_count}/{len(enhanced_articles)} ({(enhanced_count/len(enhanced_articles)*100) if enhanced_articles else 0:.1f}%)
 â€¢ Ø¹Ø¯Ø¯ Ø§Ù„Ù…Ù‚Ø§Ù„Ø§Øª ÙÙŠ Ù…Ø¯ÙˆÙ†Ø© Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬Ø§Ø¬: {len(management_articles)}
 â€¢ Ø¹Ø¯Ø¯ Ø§Ù„Ù…Ù‚Ø§Ù„Ø§Øª ÙÙŠ Ù…Ø¯ÙˆÙ†Ø© Ø§Ù„ØªØ­Ø³ÙŠÙ† ÙˆØ§Ù„ØªÙ…ÙŠØ²: {len(improvement_articles)}
 â€¢ Ù†Ø·Ø§Ù‚ Ø§Ù„ØªØºØ·ÙŠØ© Ø§Ù„Ø´Ù‡Ø±ÙŠØ©: {(datetime.now() - timedelta(days=30)).strftime('%B %d')} - {datetime.now().strftime('%B %d, %Y')}

 ðŸ“ **Ø§Ù„ØªÙ‚Ø§Ø±ÙŠØ± Ø§Ù„ØªÙŠ ØªÙ… ØªÙˆÙ„ÙŠØ¯Ù‡Ø§:**
 â€¢ Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„Ø´Ù‡Ø±ÙŠ Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© - {management_status}
 â€¢ Ø§Ù„ØªÙ‚Ø±ÙŠØ± Ø§Ù„Ø´Ù‡Ø±ÙŠ Ù„Ù„ØªÙ‚Ù†ÙŠØ© ÙˆØ§Ù„ØµØ­Ø© ÙˆØ§Ù„Ø§Ø¨ØªÙƒØ§Ø± ÙÙŠ Ø§Ù„Ø­Ø¬ - {improvement_status}

 ÙƒÙ„Ø§ Ø§Ù„ØªÙ‚Ø±ÙŠØ±ÙŠÙ† ÙŠØ­ØªÙˆÙŠØ§Ù† Ø¹Ù„Ù‰ Ø£Ù‚Ø³Ø§Ù… Ù…Ù†Ø¸Ù…Ø© ÙˆØªØ­Ù„ÙŠÙ„ Ù…ØªØ¹Ù…Ù‚ ÙˆØªÙ†Ø³ÙŠÙ‚ Ø§Ø­ØªØ±Ø§ÙÙŠ!
        """
        
        keyboard = [
            [InlineKeyboardButton("ðŸ”„ ØªÙˆÙ„ÙŠØ¯ ØªÙ‚Ø§Ø±ÙŠØ± Ø´Ù‡Ø±ÙŠØ© Ø¬Ø¯ÙŠØ¯Ø©", callback_data='generate_monthly')],
            [InlineKeyboardButton("ðŸ“° Ø§Ù„Ø£Ø®Ø¨Ø§Ø± Ø§Ù„ÙŠÙˆÙ…ÙŠØ©", callback_data='get_news')],
            [InlineKeyboardButton("ðŸ  Ø§Ù„Ù‚Ø§Ø¦Ù…Ø© Ø§Ù„Ø±Ø¦ÙŠØ³ÙŠØ©", callback_data='main_menu')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await message.edit_text(
            success_message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        
    except Exception as e:
        error_message = f"âŒ Ø­Ø¯Ø« Ø®Ø·Ø£ Ø£Ø«Ù†Ø§Ø¡ ØªÙˆÙ„ÙŠØ¯ Ø§Ù„Ù…Ø¯ÙˆÙ†Ø§Øª Ø§Ù„Ø´Ù‡Ø±ÙŠØ©: {str(e)}"
        logger.error(f"Monthly blog generation error: {str(e)}")
        await message.edit_text(error_message)

async def monthly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /monthly command directly."""
    await generate_monthly_blogs(update, context)

# ============================================================================
# AI MAGAZINE FEATURE
# ============================================================================

def scrape_og_image(article_url: str, timeout_s: int = 10) -> str:
    """
    Extract the og:image / twitter:image from an article page.
    Returns an absolute image URL or '' on failure.
    Used to get article-specific images from their source URLs before falling back to generics.
    """
    if not article_url or not isinstance(article_url, str):
        return ""
    url = article_url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return ""
    # Skip Twitter/X.com URLs - they don't return useful OG images to bots
    if "x.com" in url or "twitter.com" in url:
        return ""
    try:
        resp = requests.get(
            url,
            timeout=timeout_s,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
            },
        )
        if resp.status_code != 200 or not resp.text:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        candidates = []
        for key in ("og:image", "twitter:image", "twitter:image:src"):
            tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
            if tag and tag.get("content"):
                candidates.append(tag["content"].strip())
        link = soup.find("link", attrs={"rel": "image_src"})
        if link and link.get("href"):
            candidates.append(link["href"].strip())
        for img in candidates:
            if not img:
                continue
            if img.startswith("//"):
                return "https:" + img
            if img.startswith("http://") or img.startswith("https://"):
                return img
    except Exception:
        pass
    return ""


def generate_magazine_content_with_ai(articles):
    """
    Generate structured JSON content for the monthly Hajj report using Claude.
    Returns (magazine_data, article_map) where article_map maps 1-based index -> article metadata.
    """
    if not articles:
        return None, {}

    # Prepare article context with image URLs - store mapping for later matching
    articles_context = ""
    article_map = {}  # Map article index to image URL and source for direct lookup
    for i, article in enumerate(articles[:40]):  # Limit to 40 articles for context
        title = article.get('title', 'No title')
        content = article.get('full_content', '')[:1000]  # Truncate for token limits
        # Get image URL from various possible fields
        image_url = (
            article.get('urlToImage') or
            article.get('image_url') or
            article.get('image') or
            ''
        )
        source = article.get('source', {}).get('name', '') if isinstance(article.get('source'), dict) else str(article.get('source', ''))
        articles_context += f"Article {i+1}: {title}\nSource: {source}\nImage: {image_url}\nContent: {content}\n\n"
        # Store for direct lookup by article_index
        article_map[i + 1] = {
            'image_url': image_url,
            'source': source,
            'title': title,
            'url': article.get('url', ''),
            'raw_article': article,
        }

    system_message = (
        "You are the Editor-in-Chief of a professional monthly Hajj and Umrah report. "
        "Your goal is to maintain a professional, insightful, and visionary tone. "
        "Critical page layout rule: Each article (including the first one) must fit exactly on one A4 page. "
        "NO EXCEPTIONS - All 8 articles must be between 300-350 words TOTAL (Lead + Main Content). "
        "Strict Enforcement: Count words for each article. If any article exceeds 350 words, it will overflow the page. "
        "If any article is under 270 words, it will have excessive whitespace. "
        "Target 310-330 words per article for optimal page fill without overflow. "
        "The first article is NOT special - it must follow the same word count rules as all other articles. "
        "Balance depth with brevity - provide comprehensive coverage but adhere to the strict 300-350 word limit. "
        "Output ONLY valid JSON matching the specified structure. "
        "CRITICAL: ALL text content (titles, subtitles, leads, articles, editors_note, highlights, locations) MUST be written in MODERN STANDARD ARABIC (Ø§Ù„Ø¹Ø±Ø¨ÙŠØ© Ø§Ù„ÙØµØ­Ù‰). "
        "You may read English source articles but ALL output MUST be in Arabic."
    )

    user_prompt = f"""
    Ø£Ù†Ø´Ø¦ Ù…Ø­ØªÙˆÙ‰ Ù…Ø¬Ù„Ø© Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø§Ù„Ø´Ù‡Ø±ÙŠØ© Ø¨Ù†Ø§Ø¡Ù‹ Ø¹Ù„Ù‰ Ù‡Ø°Ù‡ Ø§Ù„Ù…Ù‚Ø§Ù„Ø§Øª:
    {articles_context}

    Ø£Ø¹Ø¯ ÙƒØ§Ø¦Ù† JSON Ø¨Ù‡Ø°Ù‡ Ø§Ù„Ø¨Ù†ÙŠØ© Ø¨Ø§Ù„Ø¶Ø¨Ø· (Ø¨Ø¯ÙˆÙ† markdownØŒ ÙÙ‚Ø· JSON):
    {{
        "title": "ØªÙ‚Ø±ÙŠØ± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©: [Ø¹Ù†ÙˆØ§Ù† Ø¬Ø°Ø§Ø¨ Ø¨Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©]",
        "subtitle": "[Ø¹Ù†ÙˆØ§Ù† ÙØ±Ø¹ÙŠ Ø¬Ø°Ø§Ø¨ Ø¨Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©]",
        "date": "[Ø§Ù„Ø´Ù‡Ø± ÙˆØ§Ù„Ø³Ù†Ø© Ø§Ù„Ø­Ø§Ù„ÙŠÙŠÙ† Ø¨Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©]",
        "highlights": [
            {{"title": "[Ø¹Ù†ÙˆØ§Ù† 1 Ø¨Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©]", "description": "[ÙˆØµÙ Ù‚ØµÙŠØ± Ø¨Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©]"}},
            {{"title": "[Ø¹Ù†ÙˆØ§Ù† 2 Ø¨Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©]", "description": "[ÙˆØµÙ Ù‚ØµÙŠØ± Ø¨Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©]"}},
            {{"title": "[Ø¹Ù†ÙˆØ§Ù† 3 Ø¨Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©]", "description": "[ÙˆØµÙ Ù‚ØµÙŠØ± Ø¨Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©]"}}
        ],
        "editors_note": "[Ø­Ø¯ Ø£Ù‚ØµÙ‰ 150 ÙƒÙ„Ù…Ø© Ø¨Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©. ØªØ¹Ù„ÙŠÙ‚ ØªØ­Ø±ÙŠØ±ÙŠ Ù…Ù‡Ù†ÙŠ ÙˆØ¨ØµÙŠØ±Ø© Ø­ÙˆÙ„ Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©.]",
        "articles": [
            {{
                "category": "[ÙˆØ§Ø­Ø¯Ø© Ù…Ù†: Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬Ø§Ø¬, Ø§Ù„ØªÙ‚Ù†ÙŠØ©, Ø§Ù„ØµØ­Ø© ÙˆØ§Ù„Ø³Ù„Ø§Ù…Ø©, Ø§Ù„ØªÙ†Ø¸ÙŠÙ… ÙˆØ§Ù„Ø¥Ø¯Ø§Ø±Ø©]",
                "title": "[Ø¹Ù†ÙˆØ§Ù† Ù…Ø¬Ù„Ø© Ø¬Ø°Ø§Ø¨ Ø¨Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©]",
                "location": "[Ø§Ù„Ù…ÙˆÙ‚Ø¹/Ø§Ù„Ù…Ù†Ø·Ù‚Ø© Ø¨Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©ØŒ Ù…Ø«Ø§Ù„: Ù…ÙƒØ© Ø§Ù„Ù…ÙƒØ±Ù…Ø© / Ø§Ù„Ø³Ø¹ÙˆØ¯ÙŠØ©]",
                "lead": "[ÙÙ‚Ø±Ø© Ø§ÙØªØªØ§Ø­ÙŠØ© Ø¬Ø°Ø§Ø¨Ø© Ø¨Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©ØŒ 2-3 Ø¬Ù…Ù„ (Ø­ÙˆØ§Ù„ÙŠ 40-50 ÙƒÙ„Ù…Ø©). Ø¹Ø¯Ø¯ Ø§Ù„ÙƒÙ„Ù…Ø§Øª Ù‡Ø°Ø§ Ù…Ø´Ù…ÙˆÙ„ ÙÙŠ Ø¥Ø¬Ù…Ø§Ù„ÙŠ 300-350.]",
                "content": "[Ø§Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„Ø±Ø¦ÙŠØ³ÙŠ Ø¨ØªÙ†Ø³ÙŠÙ‚ HTML Ø¨Ø§Ù„Ø¹Ø±Ø¨ÙŠØ© Ù…Ø¹ Ø¹Ù†Ø§ÙˆÙŠÙ† ÙØ±Ø¹ÙŠØ© <h3> ÙˆÙÙ‚Ø±Ø§Øª <p>. Ø¹Ø¯Ø¯ Ø§Ù„ÙƒÙ„Ù…Ø§Øª Ø§Ù„Ø¥Ø¬Ù…Ø§Ù„ÙŠ (Ø§Ù„Ø§ÙØªØªØ§Ø­ÙŠØ© + Ø§Ù„Ù…Ø­ØªÙˆÙ‰) ÙŠØ¬Ø¨ Ø£Ù† ÙŠÙƒÙˆÙ† 300-350 ÙƒÙ„Ù…Ø© Ø¨Ø§Ù„Ø¶Ø¨Ø·. Ø§Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„Ø±Ø¦ÙŠØ³ÙŠ 250-300 ÙƒÙ„Ù…Ø©. Ø£Ù†Ø´Ø¦ 3-4 ÙÙ‚Ø±Ø§Øª (Ø­ÙˆØ§Ù„ÙŠ 80 ÙƒÙ„Ù…Ø© Ù„ÙƒÙ„ Ù…Ù†Ù‡Ø§) Ù…Ø¹ Ø¹Ù†ÙˆØ§Ù†ÙŠÙ† ÙØ±Ø¹ÙŠÙŠÙ†.]",
                "article_index": "[Ø±Ù‚Ù… Ø§Ù„Ù…Ù‚Ø§Ù„ Ø§Ù„Ø£ØµÙ„ÙŠ Ù…Ù† Ø§Ù„Ù‚Ø§Ø¦Ù…Ø© Ø£Ø¹Ù„Ø§Ù‡ØŒ Ù…Ø«Ù„Ø§Ù‹ 3 Ø£Ùˆ 7]",
                "source": "[Ø§Ø³Ù… Ø§Ù„Ù…ØµØ¯Ø± Ø§Ù„Ø£ØµÙ„ÙŠ]",
                "score": "[Ø¯Ø±Ø¬Ø© Ø§Ù„Ø£Ù‡Ù…ÙŠØ© 1-10]"
            }},
            ... (Ø£Ù†Ø´Ø¦ Ø¨Ø§Ù„Ø¶Ø¨Ø· 8 Ù…Ù‚Ø§Ù„Ø§Øª Ù…Ù…ÙŠØ²Ø©. Ù„Ø§ ØªØªØ¬Ø§ÙˆØ² 8.)
        ]
    }}

    Ù…Ù‡Ù… Ø¬Ø¯Ø§Ù‹:
    1. ØªØ£ÙƒØ¯ Ù…Ù† Ø£Ù† Ø¬Ù…ÙŠØ¹ Ø¹Ù„Ø§Ù…Ø§Øª Ø§Ù„Ø§Ù‚ØªØ¨Ø§Ø³ Ø§Ù„Ù…Ø²Ø¯ÙˆØ¬Ø© Ø¯Ø§Ø®Ù„ Ù‚ÙŠÙ… Ø§Ù„Ù†ØµÙˆØµ Ù…Ù‡Ø±Ù‘Ø¨Ø© Ø¨Ø´ÙƒÙ„ ØµØ­ÙŠØ­ Ø¨Ø¹Ù„Ø§Ù…Ø© backslash (\\").
    2. Ù„Ø§ ØªØ³ØªØ®Ø¯Ù… ÙÙˆØ§ØµÙ„ Ø£Ø³Ø·Ø± markdown Ø£Ùˆ ÙÙˆØ§ØµÙ„ Ø²Ø§Ø¦Ø¯Ø© ØªØ¬Ø¹Ù„ JSON ØºÙŠØ± ØµØ§Ù„Ø­.
    3. ÙŠØ¬Ø¨ Ø£Ù† ÙŠÙƒÙˆÙ† Ø§Ù„Ø¥Ø®Ø±Ø§Ø¬ Ø³Ù„Ø³Ù„Ø© JSON ÙˆØ§Ø­Ø¯Ø© ØµØ§Ù„Ø­Ø©.
    4. Ø­Ù‚Ù„ article_index Ø¥Ù„Ø²Ø§Ù…ÙŠ Ù„ÙƒÙ„ Ù…Ù‚Ø§Ù„ - ÙŠØ¬Ø¨ Ø£Ù† ÙŠØ·Ø§Ø¨Ù‚ Ø±Ù‚Ù… Ø§Ù„Ù…Ù‚Ø§Ù„ ÙÙŠ Ø§Ù„Ù‚Ø§Ø¦Ù…Ø© Ø£Ø¹Ù„Ø§Ù‡ (1-40). Ù‡Ø°Ø§ ÙŠÙØ³ØªØ®Ø¯Ù… Ù„Ø±Ø¨Ø· Ø§Ù„ØµÙˆØ±Ø© Ø§Ù„ØµØ­ÙŠØ­Ø© Ø¨Ø´ÙƒÙ„ Ù…Ø¨Ø§Ø´Ø±.
    5. ØªØ·Ø¨ÙŠÙ‚ ØµØ§Ø±Ù… Ù„Ø¹Ø¯Ø¯ Ø§Ù„ÙƒÙ„Ù…Ø§Øª Ù„Ø¬Ù…ÙŠØ¹ Ø§Ù„Ù…Ù‚Ø§Ù„Ø§Øª (Ø¨Ø¯ÙˆÙ† Ø§Ø³ØªØ«Ù†Ø§Ø¡Ø§Øª):
       - Ø¥Ø¬Ù…Ø§Ù„ÙŠ Ø¹Ø¯Ø¯ Ø§Ù„ÙƒÙ„Ù…Ø§Øª Ù„ÙƒÙ„ Ù…Ù‚Ø§Ù„Ø© (Ø§Ù„Ø§ÙØªØªØ§Ø­ÙŠØ© + Ø§Ù„Ù…Ø­ØªÙˆÙ‰) ÙŠØ¬Ø¨ Ø£Ù† ÙŠÙƒÙˆÙ† Ø¨ÙŠÙ† 300-350 ÙƒÙ„Ù…Ø©.
       - Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ø¯Ù†Ù‰: 300 ÙƒÙ„Ù…Ø©.
       - Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ù‚ØµÙ‰: 350 ÙƒÙ„Ù…Ø©.
       - Ø§Ù„Ù†Ø·Ø§Ù‚ Ø§Ù„Ù…Ø«Ø§Ù„ÙŠ: 310-330 ÙƒÙ„Ù…Ø© Ù„ÙƒÙ„ Ù…Ù‚Ø§Ù„Ø©.
    6. Ø¬Ù…ÙŠØ¹ Ø§Ù„Ù†ØµÙˆØµ ÙŠØ¬Ø¨ Ø£Ù† ØªÙƒÙˆÙ† Ø¨Ø§Ù„Ù„ØºØ© Ø§Ù„Ø¹Ø±Ø¨ÙŠØ© Ø§Ù„ÙØµØ­Ù‰.
    """

    logger.info("Calling AWS Bedrock Claude API for magazine content generation...")
    content_text, error = call_claude_api(
        system_message=system_message, 
        user_message=user_prompt, 
        max_tokens=50000,
        temperature=0.7,
        use_long_timeout=True  # Use 600 second timeout for magazine generation
    )

    if error:
        logger.error(f"Magazine generation error (AWS Bedrock): {error}")
        logger.error(f"Error type: {type(error)}")
        return None

    if not content_text:
        logger.error("Magazine generation returned empty content")
        return None

    try:
        # Clean potential markdown fences
        json_str = content_text.strip()
        if json_str.startswith("```json"):
            json_str = json_str[7:]
        if json_str.startswith("```"):
            json_str = json_str[3:]
        if json_str.endswith("```"):
            json_str = json_str[:-3]
        json_str = json_str.strip()
        
        # Log the length of the response for debugging
        logger.info(f"Magazine JSON response length: {len(json_str)} characters")
        
        # Check if the JSON appears to be truncated (unterminated string or brace)
        if not json_str.endswith('}'):
            logger.warning("JSON response appears to be truncated (doesn't end with })")
            logger.error(f"JSON string ending: ...{json_str[-200:]}")
            return None
        
        magazine_data = json.loads(json_str)
        return magazine_data, article_map
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode magazine JSON: {e}")
        logger.error(f"JSON decode error at line {e.lineno}, column {e.colno}")
        logger.error(f"JSON string preview (first 500 chars):\n{json_str[:500]}")
        logger.error(f"JSON string ending (last 500 chars):\n...{json_str[-500:]}")
        logger.error(f"Full JSON length: {len(json_str)} characters")
        
        # Try to identify if this is a truncation issue
        if "Unterminated string" in str(e) or "Expecting" in str(e):
            logger.error("âš ï¸ This appears to be a truncated response. The model may have hit the max_tokens limit.")
            logger.error("   Possible solutions:")
            logger.error("   1. Reduce the number of articles in the magazine (currently 8)")
            logger.error("   2. Simplify the article content requirements")
            logger.error("   3. Split magazine generation into multiple API calls")
        
        return None, {}

def render_newspaper_pdf(content_data, output_filename="newspaper.pdf"):
    """
    Render newspaper-style PDF using Jinja2 and WeasyPrint.
    """
    if not WEASYPRINT_AVAILABLE:
        logger.error("WeasyPrint not available (missing GTK or module). Cannot generate PDF.")
        return None

    try:
        # Setup Jinja2
        template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
        env = Environment(loader=FileSystemLoader(template_dir))
        template = env.get_template('newspaper.html')
        
        # Inject default images if available
        import glob
        import random
        import pathlib
        
        images_dir = os.path.join(template_dir, 'images')
        available_images = []
        if os.path.exists(images_dir):
            all_images = (
                glob.glob(os.path.join(images_dir, '*.jpg')) +
                glob.glob(os.path.join(images_dir, '*.png')) +
                glob.glob(os.path.join(images_dir, '*.jpeg')) +
                glob.glob(os.path.join(images_dir, '*.webp'))
            )
            # Filter out cover images
            exclude_files = ['Cover.png', 'cover.png', 'cover_generated.png', 'back_cover_generated.png', 'Back Cover.png', 'Back Cover.jpg', 'back cover.png', '1767655448098.jpg','Logo.jpg']
            available_images = [
                img for img in all_images 
                if os.path.basename(img) not in exclude_files
            ]
        
        # Assign images to articles (round-robin or random)
        articles = content_data.get('articles', [])
        for article in articles:
            if available_images and not article.get('local_image_path') and not article.get('image_url'):
                # Convert to file URI safely handling spaces/OS specifics
                img_path = random.choice(available_images)
                article['local_image_path'] = pathlib.Path(img_path).as_uri()
        
        # Batch articles into pages (2 articles per page)
        pages = []
        page_num = 1
        for i in range(0, len(articles), 2):
            page_articles = articles[i:i+2]
            pages.append({
                'page_num': page_num,
                'articles': page_articles
            })
            page_num += 1
        
        # Prepare template data
        template_data = {
            'title': content_data.get('title', 'Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©'),
            'publication_name': content_data.get('publication_name', 'Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©'),
            'tagline': content_data.get('tagline', 'Ù…Ø¬Ù„Ø© Ø¥Ù„ÙƒØªØ±ÙˆÙ†ÙŠØ© ÙˆØªØ¹Ù†Ù‰ Ø¨ÙƒÙ„ Ù…Ø§ Ù‡Ùˆ ÙÙŠ Ø¹Ø§Ù„Ù… Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©'),
            'issue_number': content_data.get('issue_number', '190'),
            'pages': pages,
            'footer_text': content_data.get('footer_text', 'hajjnews'),
            'contact_phone': content_data.get('contact_phone', '00973 3701 4477'),
            'editors_note': content_data.get('editors_note', ''),
            'cover_image_path': content_data.get('cover_image_path')
        }

        # Optional cover image (look in templates/images)
        # Try Cover.png first, then fallback to other cover images
        cover_path = os.path.join(template_dir, 'images', 'Cover.png')
        if not os.path.exists(cover_path):
            cover_path = os.path.join(template_dir, 'images', '1767655448098.jpg')
        if not os.path.exists(cover_path):
            cover_path = os.path.join(template_dir, 'images', 'cover.png')
        
        if os.path.exists(cover_path):
            template_data['cover_image_path'] = pathlib.Path(cover_path).as_uri()
            logger.info(f"Using cover image: {cover_path}")
        elif content_data.get('cover_image_path'):
            template_data['cover_image_path'] = content_data.get('cover_image_path')
            logger.info(f"Using cover image from content_data")
        else:
            logger.warning("No cover image found")
        
        # Optional back cover image
        back_cover_path = os.path.join(template_dir, 'images', 'Back Cover.png')
        if not os.path.exists(back_cover_path):
            back_cover_path = os.path.join(template_dir, 'images', 'back_cover_generated.png')
        if not os.path.exists(back_cover_path):
            back_cover_path = os.path.join(template_dir, 'images', 'Back Cover.jpg')
        
        if os.path.exists(back_cover_path):
            template_data['back_cover_image_path'] = pathlib.Path(back_cover_path).as_uri()
            logger.info(f"Using back cover image: {back_cover_path}")
        elif content_data.get('back_cover_path'):
            template_data['back_cover_image_path'] = content_data.get('back_cover_path')
            logger.info(f"Using back cover image from content_data")
        
        # Render HTML
        html_out = template.render(**template_data)
        
        # Convert to PDF
        css_path = os.path.join(template_dir, 'newspaper.css')
        HTML(string=html_out, base_url=template_dir).write_pdf(
            output_filename, 
            stylesheets=[CSS(css_path)]
        )
        return output_filename
    except Exception as e:
        logger.error(f"Newspaper PDF rendering error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

def render_magazine_pdf(content_data, output_filename="magazine.pdf"):
    """
    Render PDF using Jinja2 and WeasyPrint.
    """
    if not WEASYPRINT_AVAILABLE:
        logger.error("WeasyPrint not available (missing GTK or module). Cannot generate PDF.")
        return None

    try:
        # Setup Jinja2
        template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
        env = Environment(loader=FileSystemLoader(template_dir))
        template = env.get_template('magazine.html')
        
        # Inject default images if available
        import glob
        import random
        import pathlib
        
        images_dir = os.path.join(template_dir, 'images')
        available_images = []
        if os.path.exists(images_dir):
            all_images = (
                glob.glob(os.path.join(images_dir, '*.jpg')) +
                glob.glob(os.path.join(images_dir, '*.png')) +
                glob.glob(os.path.join(images_dir, '*.jpeg')) +
                glob.glob(os.path.join(images_dir, '*.webp'))
            )
            # Filter out cover images and logo
            exclude_files = ['Cover.png', 'cover.png', 'cover_generated.png', 'back_cover_generated.png', 'Back Cover.png', 'Back Cover.jpg', 'back cover.png', '1767655448098.jpg', 'TransformiX logo .png', 'Logo.jpg']
            available_images = [
                img for img in all_images 
                if os.path.basename(img) not in exclude_files
            ]
        
        # Assign fallback local images to articles that have no real image_url
        if 'articles' in content_data:
            for article in content_data['articles']:
                if available_images and not article.get('image_url') and not article.get('local_image_path'):
                    # Only use a local fallback when no real article image is available
                    img_path = random.choice(available_images)
                    article['local_image_path'] = pathlib.Path(img_path).as_uri()
        
        # Inject Cover Image and Logo
        # Priority: cover.png (User requested)
        cover_path = os.path.join(images_dir, 'cover.png')
        if not os.path.exists(cover_path):
             cover_path = os.path.join(images_dir, 'Cover.png')
        if not os.path.exists(cover_path):
             cover_path = os.path.join(images_dir, 'cover_generated.png')
            
        if os.path.exists(cover_path):
            content_data['cover_image_path'] = pathlib.Path(cover_path).as_uri()
            
        logo_path = os.path.join(images_dir, 'TransformiX logo .png')
        if os.path.exists(logo_path):
            content_data['logo_path'] = pathlib.Path(logo_path).as_uri()

        # Inject Back Cover Image
        # Priority: Back Cover.png (User requested)
        back_cover_path = os.path.join(images_dir, 'Back Cover.png')
        if not os.path.exists(back_cover_path):
             back_cover_path = os.path.join(images_dir, 'back_cover_generated.png')
             
        if os.path.exists(back_cover_path):
            content_data['back_cover_path'] = pathlib.Path(back_cover_path).as_uri()

        # Render HTML
        html_out = template.render(**content_data)
        
        # Convert to PDF
        css_path = os.path.join(template_dir, 'magazine.css')
        HTML(string=html_out, base_url=template_dir).write_pdf(
            output_filename, 
            stylesheets=[CSS(css_path)]
        )
        return output_filename
    except Exception as e:
        logger.error(f"PDF rendering error: {e}")
        return None

def clean_deduplicate_articles(articles):
     # Simple helper if not already present
     seen = set()
     clean = []
     for a in articles:
         t = a.get('title')
         if t and t not in seen:
             seen.add(t)
             clean.append(a)
     return clean

async def generate_magazine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /magazine command."""
    user_id = get_user_id(update)
    
    # Check usage limit
    has_limit, current_usage = check_usage_limit(user_id, 'magazine')
    if not has_limit:
        limit_message = (
            f"âŒ *ØªÙ… Ø§Ù„ÙˆØµÙˆÙ„ Ø¥Ù„Ù‰ Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ù‚ØµÙ‰*\n\n"
            f"Ù„Ù‚Ø¯ Ø§Ø³ØªØ®Ø¯Ù…Øª Ø¬Ù…ÙŠØ¹ Ø§Ù„Ù…Ø­Ø§ÙˆÙ„Ø§Øª Ø§Ù„Ù…ØªØ§Ø­Ø© Ù„Ù„Ù…Ø¬Ù„Ø© ({USAGE_LIMITS['magazine']}/{USAGE_LIMITS['magazine']}).\n\n"
        )
        if update.callback_query:
            await update.callback_query.answer("ØªÙ… Ø§Ù„ÙˆØµÙˆÙ„ Ø¥Ù„Ù‰ Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ù‚ØµÙ‰", show_alert=True)
            await update.callback_query.message.reply_text(limit_message, parse_mode='Markdown')
        else:
            await update.message.reply_text(limit_message, parse_mode='Markdown')
        return
    
    # Increment usage
    increment_usage(user_id, 'magazine')
    
    # Send initial status
    if update.callback_query:
        await update.callback_query.answer()
        message = await update.callback_query.message.reply_text(
            "ðŸŽ¨ *Ù…ÙˆÙ„Ø¯ Ù…Ø¬Ù„Ø© Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©*\n\nâ³ Ø¬Ø§Ø±Ù Ø¥Ø¹Ø¯Ø§Ø¯ Ø§Ù„Ø¥ØµØ¯Ø§Ø± Ø§Ù„Ù…ÙˆØ³Ù…ÙŠ...\nðŸ” ØªØ­Ù„ÙŠÙ„ Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ù„Ù„Ø³Ù†Ø© Ø§Ù„Ù…Ø§Ø¶ÙŠØ©...",
            parse_mode='Markdown'
        )
    else:
        message = await update.message.reply_text(
            "ðŸŽ¨ *Ù…ÙˆÙ„Ø¯ Ù…Ø¬Ù„Ø© Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©*\n\nâ³ Ø¬Ø§Ø±Ù Ø¥Ø¹Ø¯Ø§Ø¯ Ø§Ù„Ø¥ØµØ¯Ø§Ø± Ø§Ù„Ù…ÙˆØ³Ù…ÙŠ...\nðŸ” ØªØ­Ù„ÙŠÙ„ Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ù„Ù„Ø³Ù†Ø© Ø§Ù„Ù…Ø§Ø¶ÙŠØ©...",
            parse_mode='Markdown'
        )

    try:
        # 1. Fetch Monthly News
        await message.edit_text("ðŸŽ¨ *Ø§Ù„Ù…Ø±Ø­Ù„Ø© 1/3:* Ø¬Ù…Ø¹ Ø§Ù„Ù…Ø¹Ù„ÙˆÙ…Ø§Øª...", parse_mode='Markdown')
        
        hajgov_articles = fetch_hajgov_news() or []
        cnn_articles = fetch_cnn_hajj_news() or []

        all_articles = clean_deduplicate_articles(hajgov_articles + cnn_articles)
        
        if not all_articles:
             await message.edit_text("âŒ Ù„Ù… ÙŠØªÙ… Ø§Ù„Ø¹Ø«ÙˆØ± Ø¹Ù„Ù‰ Ø¨ÙŠØ§Ù†Ø§Øª ÙƒØ§ÙÙŠØ© Ù„Ù„Ù…Ø¬Ù„Ø©.")
             return

        # Enhance top articles
        await message.edit_text("ðŸŽ¨ *Ø§Ù„Ù…Ø±Ø­Ù„Ø© 2/3:* ØªÙ†Ù‚ÙŠØ© ÙˆØªØ­Ø³ÙŠÙ† Ø§Ù„Ù…Ø­ØªÙˆÙ‰...", parse_mode='Markdown')
        enhanced_articles = enhance_articles_with_content(all_articles, max_articles=30, monthly_mode=True)

        # 2. Generate Content with AI - also returns article_map for direct image lookup
        await message.edit_text("ðŸŽ¨ *Ø§Ù„Ù…Ø±Ø­Ù„Ø© 3/3:* ØªØµÙ…ÙŠÙ… Ø§Ù„ØªØ®Ø·ÙŠØ· ÙˆØ¥Ù†Ø´Ø§Ø¡ PDF...", parse_mode='Markdown')
        magazine_data, article_map = generate_magazine_content_with_ai(enhanced_articles)
        
        if not magazine_data:
             await message.edit_text("âŒ ÙØ´Ù„ ÙÙŠ ØªÙˆÙ„ÙŠØ¯ Ù…Ø­ØªÙˆÙ‰ Ø§Ù„Ù…Ø¬Ù„Ø© Ø¹Ø¨Ø± Ø§Ù„Ø°ÙƒØ§Ø¡ Ø§Ù„Ø§ØµØ·Ù†Ø§Ø¹ÙŠ.")
             return

        # --- Direct image/source back-fill using article_index from AI ---
        mag_articles = magazine_data.get('articles', [])
        for mag_article in mag_articles:
            # Use article_index for direct lookup (most reliable)
            idx_raw = mag_article.get('article_index')
            try:
                idx = int(idx_raw)
            except (TypeError, ValueError):
                idx = None

            orig = article_map.get(idx) if idx else None

            # Back-fill image_url from direct index lookup
            if orig and not mag_article.get('image_url'):
                img = orig.get('image_url', '')
                if img:
                    mag_article['image_url'] = img
                    logger.info(f"Direct image match for '{mag_article.get('title','')[:50]}' via article_index={idx}")
                else:
                    # No image in fields â€” try to scrape og:image from the article URL
                    article_url = orig.get('url', '')
                    if article_url:
                        og_img = scrape_og_image(article_url)
                        if og_img:
                            mag_article['image_url'] = og_img
                            logger.info(f"OG image scraped for '{mag_article.get('title','')[:50]}': {og_img[:60]}")
                        else:
                            logger.debug(f"No OG image found for '{mag_article.get('title','')[:50]}' - will use render fallback")

        # Add magazine metadata
        current_date = datetime.now()
        magazine_data['date'] = current_date.strftime("%B %Y")
        
        # Ensure all articles have location field (default if missing)
        for article in mag_articles:
            if 'location' not in article or not article['location']:
                article['location'] = 'Ù…ÙƒØ© Ø§Ù„Ù…ÙƒØ±Ù…Ø©'

        # 3. Render PDF using NEW MAGAZINE template
        filename = f"Hajj_Umrah_{datetime.now().strftime('%B_%Y')}.pdf"
        # SWITCHED from render_newspaper_pdf to render_magazine_pdf
        pdf_path = render_magazine_pdf(magazine_data, filename)
        
        if pdf_path and os.path.exists(pdf_path):
             await message.reply_document(
                document=open(pdf_path, 'rb'),
                filename=filename,
                caption=f"ðŸŽ¨ **Ù…Ø¬Ù„Ø© Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© - {datetime.now().strftime('%B %Y')}**\n\nØ§Ø³ØªÙ…ØªØ¹ Ø¨ØªÙ‚Ø±ÙŠØ±Ùƒ Ø§Ù„Ù…ÙˆØ³Ù…ÙŠ!",
                parse_mode='Markdown'
            )
             # Optional: os.unlink(pdf_path) if running long term
        else:
             await message.edit_text("âŒ ÙØ´Ù„ ÙÙŠ Ø¥Ù†Ø´Ø§Ø¡ Ù…Ù„Ù PDF.")

    except Exception as e:
        logger.error(f"Magazine error: {e}")
        await message.edit_text(f"âŒ Ø®Ø·Ø£: {str(e)}")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset usage limits for users (admin only)."""
    user_id = get_user_id(update)
    
    # Check if user is admin
    if ADMIN_USER_IDS and user_id not in ADMIN_USER_IDS:
        await update.message.reply_text(
            "âŒ *ØºÙŠØ± Ù…ØµØ±Ø­*\n\nÙ‡Ø°Ø§ Ø§Ù„Ø£Ù…Ø± Ù…ØªØ§Ø­ Ù„Ù„Ù…Ø³Ø¤ÙˆÙ„ÙŠÙ† ÙÙ‚Ø·.",
            parse_mode='Markdown'
        )
        return
    
    # Check if specific user ID provided
    if context.args and len(context.args) > 0:
        try:
            target_user_id = int(context.args[0])
            if reset_user_usage(target_user_id):
                await update.message.reply_text(
                    f"âœ… ØªÙ… Ø¥Ø¹Ø§Ø¯Ø© ØªØ¹ÙŠÙŠÙ† Ø§Ù„Ù…Ø­Ø§ÙˆÙ„Ø§Øª Ù„Ù„Ù…Ø³ØªØ®Ø¯Ù…: {target_user_id}",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text(
                    f"âŒ Ù„Ù… ÙŠØªÙ… Ø§Ù„Ø¹Ø«ÙˆØ± Ø¹Ù„Ù‰ Ø§Ù„Ù…Ø³ØªØ®Ø¯Ù…: {target_user_id}",
                    parse_mode='Markdown'
                )
        except ValueError:
            await update.message.reply_text(
                "âŒ Ù…Ø¹Ø±Ù‘Ù Ø§Ù„Ù…Ø³ØªØ®Ø¯Ù… ØºÙŠØ± ØµØ­ÙŠØ­. Ø§Ø³ØªØ®Ø¯Ù…: `/reset [user_id]` Ø£Ùˆ `/reset all`",
                parse_mode='Markdown'
            )
    elif context.args and context.args[0].lower() == 'all':
        reset_user_usage()
        await update.message.reply_text(
            "âœ… ØªÙ… Ø¥Ø¹Ø§Ø¯Ø© ØªØ¹ÙŠÙŠÙ† Ø¬Ù…ÙŠØ¹ Ø§Ù„Ù…Ø­Ø§ÙˆÙ„Ø§Øª Ù„Ø¬Ù…ÙŠØ¹ Ø§Ù„Ù…Ø³ØªØ®Ø¯Ù…ÙŠÙ†.",
            parse_mode='Markdown'
        )
    else:
        # Reset current user
        reset_user_usage(user_id)
        await update.message.reply_text(
            "âœ… ØªÙ… Ø¥Ø¹Ø§Ø¯Ø© ØªØ¹ÙŠÙŠÙ† Ù…Ø­Ø§ÙˆÙ„Ø§ØªÙƒ.",
            parse_mode='Markdown'
        )

async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current usage status."""
    user_id = get_user_id(update)
    status = get_usage_status(user_id)
    
    status_message = (
        "ðŸ“Š *Ø­Ø§Ù„Ø© Ø§Ù„Ø§Ø³ØªØ®Ø¯Ø§Ù… Ø§Ù„Ø­Ø§Ù„ÙŠØ©*\n\n"
        f"ðŸ“° Ø§Ù„Ø£Ø®Ø¨Ø§Ø± Ø§Ù„ÙŠÙˆÙ…ÙŠØ©: {status['daily_news']['used']}/{status['daily_news']['limit']}\n"
        f"ðŸ“ Ø§Ù„ØªÙ‚Ø§Ø±ÙŠØ± Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹ÙŠØ©: {status['weekly']['used']}/{status['weekly']['limit']}\n"
        f"ðŸ“… Ø§Ù„ØªÙ‚Ø§Ø±ÙŠØ± Ø§Ù„Ø´Ù‡Ø±ÙŠØ©: {status['monthly']['used']}/{status['monthly']['limit']}\n"
        f"ðŸŽ¨ Ø§Ù„Ù…Ø¬Ù„Ø©: {status['magazine']['used']}/{status['magazine']['limit']}\n\n"
        f"Ø§Ø³ØªØ®Ø¯Ù… `/reset` Ù„Ø¥Ø¹Ø§Ø¯Ø© ØªØ¹ÙŠÙŠÙ† Ø§Ù„Ù…Ø­Ø§ÙˆÙ„Ø§Øª (Ù„Ù„Ù…Ø³Ø¤ÙˆÙ„ÙŠÙ† ÙÙ‚Ø·)."
    )
    
    await update.message.reply_text(status_message, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /help is issued."""
    help_text = """
â­ *Ù…Ø³Ø§Ø¹Ø¯Ø© Ø¨ÙˆØª Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø§Ù„Ù…Ø­Ø³Ù‘Ù†Ø©*

*ðŸ†• Ø§Ù„Ù…Ø²Ø§ÙŠØ§ Ø§Ù„Ù…Ø­Ø³Ù‘Ù†Ø©:*
â€¢ ðŸ“– **Ø§Ø³ØªØ®Ø±Ø§Ø¬ ÙƒØ§Ù…Ù„ Ù„Ù„Ù…Ù‚Ø§Ù„Ø§Øª** â€“ Ù‚Ø±Ø§Ø¡Ø© Ø§Ù„Ù†Øµ Ø§Ù„ÙƒØ§Ù…Ù„ Ù„Ù„Ù…Ù‚Ø§Ù„Ø§Øª ÙˆÙ„ÙŠØ³ Ø§Ù„ÙˆØµÙ ÙÙ‚Ø·  
â€¢ ðŸ§  **Ù…Ù„Ø®ØµØ§Øª Ø£Ø°ÙƒÙ‰** â€“ ØªØ­Ù„ÙŠÙ„ ÙŠØ¹ØªÙ…Ø¯ Ø¹Ù„Ù‰ Ø§Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„ÙƒØ§Ù…Ù„  
â€¢ ðŸ“ **ØªÙˆÙ„ÙŠØ¯ Ù…Ø¯ÙˆÙ†Ø§Øª Ø£Ø³Ø¨ÙˆØ¹ÙŠØ© ÙˆØ´Ù‡Ø±ÙŠØ©** â€“ ØªÙ‚Ø§Ø±ÙŠØ± Ù…Ø¹Ù…Ù‘Ù‚Ø© Ø¹Ù† Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©  
â€¢ ðŸŽ¨ **ØªÙˆÙ„ÙŠØ¯ Ù…Ø¬Ù„Ø© Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø§Ù„Ø´Ù‡Ø±ÙŠØ©** â€“ Ù…Ø¬Ù„Ø© PDF Ø§Ø­ØªØ±Ø§ÙÙŠØ© Ø¨ØªØµÙ…ÙŠÙ… Ø¬Ù…ÙŠÙ„
â€¢ ðŸ” **Ø§Ø³ØªØ®Ø±Ø§Ø¬ Ù…ØªØ¹Ø¯Ø¯ Ø§Ù„Ø£Ø³Ø§Ù„ÙŠØ¨** â€“ Ø§Ø³ØªØ®Ø¯Ø§Ù… newspaper3k Ùˆ BeautifulSoup  
â€¢ ðŸ“Š **Ø¥Ø­ØµØ§Ø¦ÙŠØ§Øª Ø§Ù„Ù…Ø­ØªÙˆÙ‰** â€“ Ø¹Ø±Ø¶ Ù†Ø³Ø¨Ø© Ù†Ø¬Ø§Ø­ Ø§Ø³ØªØ®Ø±Ø§Ø¬ Ø§Ù„Ù†ØµÙˆØµ  
â€¢ ðŸŽ¯ **ÙÙ„ØªØ±Ø© Ù…ÙˆØ¬Ù‡Ø© Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©** â€“ Ø§Ø³ØªØ¨Ø¹Ø§Ø¯ Ø§Ù„Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø±ÙŠØ§Ø¶ÙŠØ© ÙˆØ§Ù„Ø¬Ø±Ø§Ø¦Ù… ÙˆØºÙŠØ± Ø°Ø§Øª Ø§Ù„ØµÙ„Ø©

*Ø§Ù„Ø£ÙˆØ§Ù…Ø± Ø§Ù„Ù…ØªØ§Ø­Ø©:*
â€¢ `/start` â€“ Ø±Ø³Ø§Ù„Ø© Ø§Ù„ØªØ±Ø­ÙŠØ¨ ÙˆØ§Ù„Ù‚Ø§Ø¦Ù…Ø© Ø§Ù„Ø±Ø¦ÙŠØ³ÙŠØ©  
â€¢ `/news` â€“ Ø§Ù„Ø­ØµÙˆÙ„ Ø¹Ù„Ù‰ Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø§Ù„Ù…Ø­Ø³Ù‘Ù†Ø© Ù…Ø¹ Ø§Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„ÙƒØ§Ù…Ù„  
â€¢ `/categories` â€“ ØªØµÙØ­ Ø§Ù„Ø£Ø®Ø¨Ø§Ø± Ø­Ø³Ø¨ Ø§Ù„ØªØµÙ†ÙŠÙ  
â€¢ `/weekly` â€“ ØªÙˆÙ„ÙŠØ¯ ØªÙ‚Ø§Ø±ÙŠØ±/Ù…Ø¯ÙˆÙ†Ø§Øª Ø£Ø³Ø¨ÙˆØ¹ÙŠØ© Ø´Ø§Ù…Ù„Ø©  
â€¢ `/monthly` â€“ ØªÙˆÙ„ÙŠØ¯ ØªÙ‚Ø§Ø±ÙŠØ±/Ù…Ø¯ÙˆÙ†Ø§Øª Ø´Ù‡Ø±ÙŠØ© Ø´Ø§Ù…Ù„Ø©  
â€¢ `/magazine` â€“ ØªÙˆÙ„ÙŠØ¯ Ù…Ø¬Ù„Ø© Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ø§Ù„Ø´Ù‡Ø±ÙŠØ© Ø§Ø­ØªØ±Ø§ÙÙŠØ© (PDF)
â€¢ `/keywords` â€“ Ø¥Ø¹Ø¯Ø§Ø¯ Ø§Ù„ÙƒÙ„Ù…Ø§Øª Ø§Ù„Ù…ÙØªØ§Ø­ÙŠØ© Ø§Ù„Ø£Ø³Ø§Ø³ÙŠØ© ÙˆØ§Ù„Ø«Ø§Ù†ÙˆÙŠØ© (Ø¨Ø§Ù„Ø¥Ù†Ø¬Ù„ÙŠØ²ÙŠØ©) Ù„ØªØ­Ø³ÙŠÙ† Ù…Ø­Ø±ÙƒØ§Øª Ø§Ù„Ø¨Ø­Ø«  
â€¢ `/help` â€“ Ø¹Ø±Ø¶ Ø±Ø³Ø§Ù„Ø© Ø§Ù„Ù…Ø³Ø§Ø¹Ø¯Ø© Ù‡Ø°Ù‡

*ÙƒÙŠÙ ÙŠØ¹Ù…Ù„ Ø§Ù„Ø§Ø³ØªØ®Ø±Ø§Ø¬ Ø§Ù„Ù…Ø­Ø³Ù‘Ù† Ù„Ù„Ù…Ø­ØªÙˆÙ‰:*
1. ðŸ“¡ Ø¬Ù„Ø¨ Ø§Ù„Ø£Ø®Ø¨Ø§Ø± Ù…Ù† NewsAPI Ùˆ GNews ÙˆØªØºØ°ÙŠØ§Øª RSS Ø§Ù„Ù…ØªØ®ØµØµØ©  
2. ðŸ” Ø§Ø³ØªØ®Ø±Ø§Ø¬ Ø§Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„ÙƒØ§Ù…Ù„ Ù…Ù† Ø§Ù„Ø±ÙˆØ§Ø¨Ø·  
3. ðŸ“– Ø§Ø³ØªØ®Ø¯Ø§Ù… Ø·Ø±ÙŠÙ‚ØªÙŽÙŠ newspaper3k Ùˆ BeautifulSoup  
4. ðŸ§  ØªØ·Ø¨ÙŠÙ‚ ÙÙ„ØªØ±Ø© Ù…ÙˆØ¬Ù‡Ø© Ù„Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø© Ù„Ø¥Ø²Ø§Ù„Ø© Ø§Ù„Ø¶Ø¬ÙŠØ¬  
5. ðŸ“„ Ø¥Ù†Ø´Ø§Ø¡ ØªÙ‚Ø§Ø±ÙŠØ± ØªÙØµÙŠÙ„ÙŠØ© ÙˆÙ…Ù„ÙØ§Øª PDF

*Ø§Ù„ØªØµÙ†ÙŠÙØ§Øª Ø§Ù„Ù…ØªØ§Ø­Ø©:*
â€¢ ðŸ“Š Ø®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬Ø§Ø¬  
â€¢ ðŸ† Ù…Ø¹Ø§ÙŠÙŠØ± ISO ÙˆØ§Ù„Ø´Ù‡Ø§Ø¯Ø§Øª  
â€¢ â­ Ø£Ø·Ø± Ø§Ù„ØªÙ…ÙŠØ² ÙˆØ§Ù„Ø¬ÙˆØ§Ø¦Ø²  
â€¢ ðŸ”„ ØªØ­Ø³ÙŠÙ† Ø§Ù„Ø¹Ù…Ù„ÙŠØ§Øª ÙˆØ§Ù„Ù„ÙŠÙ†  
â€¢ ðŸ“° Ø£Ø®Ø¨Ø§Ø± Ø¹Ø§Ù…Ø©  

*ÙÙˆØ§Ø¦Ø¯ Ø§Ø³ØªØ®Ø¯Ø§Ù… Ø§Ù„Ù…Ø­ØªÙˆÙ‰ Ø§Ù„ÙƒØ§Ù…Ù„:*
â€¢ Ù…Ù„Ø®ØµØ§Øª Ø£ÙƒØ«Ø± Ø¯Ù‚Ø©  
â€¢ ØªØµÙ†ÙŠÙ Ø£ÙØ¶Ù„ Ù„Ù„Ù…Ù‚Ø§Ù„Ø§Øª  
â€¢ Ø±Ø¤Ù‰ ÙˆØªØ­Ù„ÙŠÙ„Ø§Øª Ø£Ø¹Ù…Ù‚  
â€¢ ÙÙ‡Ù… ÙƒØ§Ù…Ù„ Ù„Ù„Ø³ÙŠØ§Ù‚  
â€¢ ØªÙ‚Ø§Ø±ÙŠØ± Ø§Ø­ØªØ±Ø§ÙÙŠØ© Ù‚Ø§Ø¨Ù„Ø© Ù„Ù„Ù…Ø´Ø§Ø±ÙƒØ©  
â€¢ Ø¯Ø¹Ù… ØªÙˆÙ„ÙŠØ¯ Ù…Ø¯ÙˆÙ†Ø§Øª Ø£Ø³Ø¨ÙˆØ¹ÙŠØ© ÙˆØ´Ù‡Ø±ÙŠØ©

Ø§Ø³ØªØ®Ø¯Ù… `/news` Ù„Ù„ØªØ­Ø¯ÙŠØ«Ø§Øª Ø§Ù„ÙŠÙˆÙ…ÙŠØ©ØŒ Ùˆ`/weekly` Ù„Ù„ØªÙ‚Ø§Ø±ÙŠØ± Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹ÙŠØ©ØŒ Ùˆ`/monthly` Ù„Ù„ØªÙ‚Ø§Ø±ÙŠØ± Ø§Ù„Ø´Ù‡Ø±ÙŠØ©ØŒ Ùˆ`/magazine` Ù„Ù„Ù…Ø¬Ù„Ø© Ø§Ù„Ø´Ù‡Ø±ÙŠØ© Ø§Ù„Ø§Ø­ØªØ±Ø§ÙÙŠØ©.
    """
    
    # Handle both regular commands and callback queries
    if update.callback_query:
        await update.callback_query.message.reply_text(help_text, parse_mode='Markdown')
    else:
        await update.message.reply_text(help_text, parse_mode='Markdown')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /start is issued."""
    welcome_message = """
â­ ðŸ‘‹ Ù…Ø±Ø­Ø¨Ø§Ù‹ Ø¨Ùƒ! Ø£Ù†Ø§ Ù…Ø³Ø§Ø¹Ø¯Ùƒ Ø§Ù„Ø¥Ø®Ø¨Ø§Ø±ÙŠ Ø§Ù„Ø°ÙƒÙŠ Ù„Ù‚Ø·Ø§Ø¹ Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©
ØªÙ… ØªØµÙ…ÙŠÙ…ÙŠ Ø®ØµÙŠØµØ§Ù‹ Ù„Ø£ÙƒÙˆÙ† Ø±ÙÙŠÙ‚Ùƒ Ø§Ù„ÙŠÙˆÙ…ÙŠ ÙÙŠ Ù…ØªØ§Ø¨Ø¹Ø© ÙƒÙ„ Ù…Ø§ ÙŠØ®Øµ Ø£Ø®Ø¨Ø§Ø± ÙˆØ®Ø¯Ù…Ø§Øª Ø§Ù„Ø­Ø¬ ÙˆØ§Ù„Ø¹Ù…Ø±Ø©.
Ø£Ù‚ÙˆÙ… Ø¨Ø¬Ù…Ø¹ Ø£Ø­Ø¯Ø« Ø§Ù„Ù…Ø³ØªØ¬Ø¯Ø§ØªØŒ ØªØ­Ù„ÙŠÙ„Ù‡Ø§ØŒ ÙˆØªÙ„Ø®ÙŠØµÙ‡Ø§ Ù„Ùƒ Ø¨Ø¯Ù‚Ø© ÙˆØ§Ø­ØªØ±Ø§ÙÙŠØ© Ø¹Ø§Ù„ÙŠØ©ØŒ
Ù„ØªÙƒÙˆÙ† Ø¯Ø§Ø¦Ù…Ø§Ù‹ ÙÙŠ Ù‚Ù„Ø¨ Ø§Ù„Ø­Ø¯Ø« Ø¯ÙˆÙ† Ø¥Ù‡Ø¯Ø§Ø± ÙˆÙ‚ØªÙƒ ÙÙŠ Ø§Ù„Ø¨Ø­Ø« Ø¨ÙŠÙ† Ø§Ù„Ù…ØµØ§Ø¯Ø± Ø§Ù„Ù…ØªØ¹Ø¯Ø¯Ø©.

ðŸ¤– Ù…Ù„Ø§Ø­Ø¸Ø© Ù‡Ø§Ù…Ø©:
Ø£Ø¹ØªÙ…Ø¯ Ø¹Ù„Ù‰ Ø®ÙˆØ§Ø±Ø²Ù…ÙŠØ§Øª Ø§Ù„Ø°ÙƒØ§Ø¡ Ø§Ù„Ø§ØµØ·Ù†Ø§Ø¹ÙŠ Ø§Ù„Ù…ØªÙ‚Ø¯Ù…Ø© Ù„Ù…Ø¹Ø§Ù„Ø¬Ø© ÙˆØªÙ„Ø®ÙŠØµ Ø§Ù„Ø£Ø®Ø¨Ø§Ø±.
(Ù‡Ø°Ù‡ Ø§Ù„Ø®Ø¯Ù…Ø© ØªÙ‡Ø¯Ù Ù„ØªØ³Ù‡ÙŠÙ„ Ø§Ù„Ù…ØªØ§Ø¨Ø¹Ø© ÙˆÙ„Ø§ ØªØ¹ØªØ¨Ø± Ø¨Ø¯ÙŠÙ„Ø§Ù‹ Ø¹Ù† Ø§Ù„ØªØµØ±ÙŠØ­Ø§Øª ÙˆØ§Ù„Ù‚Ø±Ø§Ø±Ø§Øª Ø§Ù„Ø±Ø³Ù…ÙŠØ©).

âœ¨ Ø£Ø¨Ø±Ø² Ù…Ø§ Ø£ÙˆÙØ±Ù‡ Ù„Ùƒ:
ðŸ“° Ù…Ù„Ø®ØµØ§Øª ÙŠÙˆÙ…ÙŠØ© Ù„Ø£Ù‡Ù… ÙˆØ£Ø­Ø¯Ø« Ø£Ø®Ø¨Ø§Ø± Ø§Ù„Ù‚Ø·Ø§Ø¹.
ðŸ“Š ØªÙ‚Ø§Ø±ÙŠØ± ØªØ­Ù„ÙŠÙ„ÙŠØ© Ø´Ø§Ù…Ù„Ø© ÙˆÙ…ÙØµÙ„Ø© (Ø£Ø³Ø¨ÙˆØ¹ÙŠØ© ÙˆØ´Ù‡Ø±ÙŠØ©).
ðŸ“˜ Ø¥ØµØ¯Ø§Ø±Ø§Øª Ø´Ù‡Ø±ÙŠØ© Ù…ØªÙƒØ§Ù…Ù„Ø© Ø¨ØµÙŠØºØ© PDF Ø¬Ø§Ù‡Ø²Ø© Ù„Ù„Ù…Ø´Ø§Ø±ÙƒØ©.
â±ï¸ ØªÙˆÙÙŠØ± Ø§Ù„Ø¬Ù‡Ø¯ ÙˆØ§Ù„ÙˆÙ‚Øª Ù„ØªØ¨Ù‚ÙŽ Ù…Ø·Ù„Ø¹Ø§Ù‹ Ø¹Ù„Ù‰ Ù…Ø¯Ø§Ø± Ø§Ù„Ø³Ø§Ø¹Ø©.

ðŸŽ¯ Ù„Ù…Ø§Ø°Ø§ ØªØ­ØªØ§Ø¬Ù†ÙŠØŸ
â€¢ Ù„ØªÙƒÙˆÙ† Ø¹Ù„Ù‰ Ø¯Ø±Ø§ÙŠØ© ØªØ§Ù…Ø© Ø¨Ù…ØªØºÙŠØ±Ø§Øª Ø§Ù„Ø³ÙˆÙ‚ Ø¨Ø´ÙƒÙ„ ÙÙˆØ±ÙŠ.
â€¢ Ù„ØªØ²ÙˆÙŠØ¯ ÙØ±ÙŠÙ‚ Ø¹Ù…Ù„Ùƒ ÙˆØ¹Ù…Ù„Ø§Ø¦Ùƒ Ø¨ØªÙ‚Ø§Ø±ÙŠØ± Ø¯ÙˆØ±ÙŠØ© Ø§Ø­ØªØ±Ø§ÙÙŠØ© ÙˆÙ…ÙˆØ«ÙˆÙ‚Ø©.
â€¢ Ù„Ø¯Ø¹Ù… Ø§Ø¬ØªÙ…Ø§Ø¹Ø§ØªÙƒ Ø§Ù„Ø¥Ø¯Ø§Ø±ÙŠØ© Ø¨Ù…Ù„Ø®ØµØ§Øª Ø¯Ù‚ÙŠÙ‚Ø© Ø¬Ø§Ù‡Ø²Ø© Ù„Ù„Ø§Ø³ØªØ®Ø¯Ø§Ù….

ðŸš€ Ø¬Ø§Ù‡Ø² Ù„Ù„Ø¨Ø¯Ø¡ØŸ
Ø§Ø³ØªØ®Ø¯Ù… Ø§Ù„Ø®ÙŠØ§Ø±Ø§Øª ÙˆØ§Ù„Ø£Ø²Ø±Ø§Ø± Ø¨Ø§Ù„Ø£Ø³ÙÙ„ Ù„Ø§Ø³ØªÙƒØ´Ø§Ù Ø§Ù„Ø£Ø®Ø¨Ø§Ø± ÙˆØ§Ù„ØªÙ‚Ø§Ø±ÙŠØ±.
    """

    
    keyboard = [
        [InlineKeyboardButton("ðŸ“° Ø§Ù„Ù…Ù„Ø®Øµ Ø§Ù„ÙŠÙˆÙ…ÙŠ", callback_data='get_news')],
        [InlineKeyboardButton("ðŸ“Š Ø§Ù„Ù…Ù„Ø®Øµ Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹ÙŠ", callback_data='generate_weekly'),
         InlineKeyboardButton("ðŸ“… Ø§Ù„Ù…Ù„Ø®Øµ Ø§Ù„Ø´Ù‡Ø±ÙŠ", callback_data='generate_monthly')],
        [InlineKeyboardButton("ðŸ“° Ø§Ù„Ù…Ø¬Ù„Ø©", callback_data='generate_magazine')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Handle both regular messages and callback queries
    if update.callback_query:
        await update.callback_query.message.reply_text(
            welcome_message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            welcome_message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

def main():
    """Start the Hajj and Umrah news bot."""
    # Create the Application
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("news", get_news))
    application.add_handler(CommandHandler("categories", show_categories))
    application.add_handler(CommandHandler("weekly", weekly_command))  # Weekly blog command
    application.add_handler(CommandHandler("monthly", monthly_command))  # Monthly blog command
    application.add_handler(CommandHandler("magazine", generate_magazine))  # Magazine command
    application.add_handler(CommandHandler("keywords", keywords_command))
    application.add_handler(CommandHandler("setkeywords", keywords_command))
    application.add_handler(CommandHandler("reset", reset_command))  # Reset usage command
    application.add_handler(CommandHandler("usage", usage_command))  # Show usage status
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Start the bot
    print("â­ Starting Enhanced Hajj and Umrah News Bot...")
    print("ðŸ“± Bot is ready! Send /start to begin.")
    print("âœ¨ Enhanced features:")
    print("   â€¢ ðŸ“– Full article content extraction")
    print("   â€¢ ðŸ§  Hajj and Umrah-specific filtering")
    print("   â€¢ ðŸ“ Weekly & monthly blog generation")
    print("   â€¢ ðŸ“„ Enhanced reports with full content")
    print("   â€¢ ðŸ” Multi-method content extraction")
    print("   â€¢ ðŸ“Š Content extraction statistics")
    print("   â€¢ âš¡ Smart categorization using full text")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
