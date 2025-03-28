"""
Module for collecting and processing AI/ML research papers from ArXiv
"""
import os
import time
import json
import logging
import sqlite3
import hashlib
import requests
import PyPDF2
import feedparser
import Levenshtein
from datetime import datetime
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import re

# Import arxiv
try:
    import arxiv
    ARXIV_AVAILABLE = True
except ImportError:
    ARXIV_AVAILABLE = False

import config
try:
    from mistral_ocr import mistral_ocr
except ImportError:
    mistral_ocr = None

# Import Mistral AI library if configured
try:
    from mistralai import Mistral
    from mistralai.models import DocumentURLChunk, TextChunk, ImageURLChunk
    MISTRAL_AVAILABLE = True
except ImportError:
    MISTRAL_AVAILABLE = False
    logging.warning("Mistral AI library not available. Install with 'pip install mistralai'")

# Configure logging
log_dir = os.path.join(config.DATA_DIR, 'logs')
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(log_dir, 'arxiv_collector.log')),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('arxiv_collector')

# Headers for requests
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
}

def setup_directories():
    """Create necessary directories for storing paper data"""
    papers_dir = os.path.join(config.DATA_DIR, "papers")
    papers_pdf_dir = os.path.join(papers_dir, "pdf")
    papers_text_dir = os.path.join(papers_dir, "text")
    
    os.makedirs(papers_dir, exist_ok=True)
    os.makedirs(papers_pdf_dir, exist_ok=True)
    os.makedirs(papers_text_dir, exist_ok=True)
    
    return papers_dir, papers_pdf_dir, papers_text_dir

def get_proxy():
    """
    Get a proxy from the configuration
    Returns:
        dict or None: Proxy dictionary for requests or None if proxies not enabled
    """
    if not hasattr(config, 'PROXY_CONFIG') or not config.PROXY_CONFIG.get('enabled', False):
        return None
    
    if hasattr(config, 'get_proxy') and callable(config.get_proxy):
        # Use the proxy rotation function if available
        proxy_url = config.get_proxy()
        if not proxy_url:
            return None
            
        return {
            "http": proxy_url,
            "https": proxy_url
        }
    
    # Use the first proxy from the list if available
    if hasattr(config, 'PROXY_SERVERS') and config.PROXY_SERVERS:
        proxy_url = config.PROXY_SERVERS[0]
        return {
            "http": proxy_url,
            "https": proxy_url
        }
        
    # Use the configured proxy settings
    proxy_config = config.PROXY_CONFIG
    proxy_url = f"{proxy_config['protocol']}://{proxy_config['username']}:{proxy_config['password']}@{proxy_config['host']}:{proxy_config['port']}"
    
    return {
        "http": proxy_url,
        "https": proxy_url
    }

def extract_text_from_pdf(pdf_path):
    """
    Extract text content from a PDF file using PyPDF2
    Falls back to default PyPDF2 extraction if Mistral OCR is not available
    """
    # Check if Mistral OCR is enabled and available
    use_mistral = (hasattr(config, 'MISTRAL_OCR_CONFIG') and 
                  config.MISTRAL_OCR_CONFIG.get('enabled', False) and 
                  MISTRAL_AVAILABLE)
                  
    if use_mistral:
        # Try Mistral OCR first
        try:
            return extract_text_with_mistral_ocr(pdf_path)
        except Exception as e:
            logger.warning(f"Mistral OCR failed, falling back to PyPDF2: {str(e)}")
            # Fall back to PyPDF2 if configured to do so
            if config.MISTRAL_OCR_CONFIG.get('fallback_to_pypdf', True):
                return extract_text_with_pypdf2(pdf_path)
            else:
                logger.error(f"Mistral OCR failed and fallback disabled: {str(e)}")
                return None
    else:
        # Use PyPDF2 by default
        return extract_text_with_pypdf2(pdf_path)

def extract_text_with_pypdf(pdf_path):
    """Extract text content from a PDF file using PyPDF2"""
    try:
        text = ""
        with open(pdf_path, 'rb') as file:
            reader = PyPDF2.PdfReader(file)
            num_pages = len(reader.pages)
            
            # Extract text from each page
            for page_num in range(num_pages):
                page = reader.pages[page_num]
                text += page.extract_text() + "\n\n"
                
            logger.info(f"Extracted text from {pdf_path} ({num_pages} pages)")
        return text
    except Exception as e:
        logger.error(f"Error extracting text from PDF {pdf_path} with PyPDF2: {str(e)}")
        return None

def extract_text_with_pypdf2(pdf_path):
    """
    Extract text from a PDF file using PyPDF2
    
    Args:
        pdf_path: Path to the PDF file
        
    Returns:
        str: Extracted text
    """
    try:
        text = ""
        title = None
        
        with open(pdf_path, 'rb') as f:
            pdf_reader = PyPDF2.PdfReader(f)
            num_pages = len(pdf_reader.pages)
            
            # Extract title from first page if possible
            if pdf_reader.pages:
                first_page_text = pdf_reader.pages[0].extract_text() or ""
                lines = first_page_text.split('\n')
                if lines:
                    # Use first non-empty line as title
                    for line in lines:
                        if line.strip():
                            title = line.strip()
                            break
            
            # Extract text from all pages
            for page in pdf_reader.pages:
                try:
                    page_text = page.extract_text() or ""
                    # Handle encoding issues
                    page_text = page_text.encode('utf-8', errors='replace').decode('utf-8')
                    text += page_text + "\n\n"
                except Exception as e:
                    logger.warning(f"Error extracting text from page: {e}")
                    continue
                
        logger.info(f"Extracted text using PyPDF2: {len(text)} characters from {num_pages} pages")
        return text
    except Exception as e:
        logger.error(f"Error extracting text with PyPDF2: {e}")
        return ""

def extract_text_with_mistral_ocr(pdf_path):
    """
    Extract text from a PDF file using Mistral OCR
    
    Args:
        pdf_path: Path to the PDF file
        
    Returns:
        str: Extracted text
    """
    from config import MISTRAL_API_KEY, MISTRAL_OCR_CONFIG
    
    if not mistral_ocr:
        logger.warning("Mistral OCR not available, falling back to PyPDF2")
        return extract_text_with_pypdf2(pdf_path)
        
    try:
        # Get the API key from config
        api_key = MISTRAL_API_KEY
        
        # Get the model from config
        model = MISTRAL_OCR_CONFIG.get('model', 'mistral-large-pdf')
        
        # Extract text using Mistral OCR
        result = mistral_ocr.extract_text(pdf_path, api_key=api_key, model=model)
        
        # Handle different return formats
        if isinstance(result, tuple) and len(result) > 0:
            # If returning (success, text) tuple or (text, title) tuple
            text = result[1] if isinstance(result[0], bool) else result[0]
            success = result[0] if isinstance(result[0], bool) else True
        else:
            # If just returning text
            text = result
            success = bool(text)
        
        if success and text:
            logger.info(f"Successfully extracted text from PDF with Mistral OCR: {os.path.basename(pdf_path)}")
            return text
        else:
            logger.warning(f"Mistral OCR extraction failed for {pdf_path}")
            
            # Fall back to PyPDF2 if configured to do so
            if MISTRAL_OCR_CONFIG.get('fallback_to_pypdf', True):
                return extract_text_with_pypdf2(pdf_path)
            else:
                logger.error(f"Mistral OCR failed and fallback disabled")
                return ""
    except Exception as e:
        logger.error(f"Error using Mistral OCR: {e}")
        
        # Fall back to PyPDF2 if configured to do so
        if MISTRAL_OCR_CONFIG.get('fallback_to_pypdf', True):
            return extract_text_with_pypdf2(pdf_path)
        else:
            logger.error(f"Mistral OCR failed and fallback disabled: {e}")
            return ""

def parse_sections(text):
    """Attempt to parse sections from extracted PDF text"""
    sections = {
        "abstract": "",
        "introduction": "",
        "methodology": "",
        "results": "",
        "conclusion": "",
        "references": ""
    }
    
    if not text:
        return sections
        
    # Simple heuristic section detection
    lines = text.split('\n')
    current_section = "abstract"
    
    for line in lines:
        line = line.strip()
        line_lower = line.lower()
        
        # Check for section headers
        if "abstract" in line_lower and len(line) < 30:
            current_section = "abstract"
            continue
        elif any(x in line_lower for x in ["introduction", "background"]) and len(line) < 30:
            current_section = "introduction"
            continue
        elif any(x in line_lower for x in ["method", "approach", "model", "implementation"]) and len(line) < 30:
            current_section = "methodology"
            continue
        elif any(x in line_lower for x in ["result", "evaluation", "experiment", "performance"]) and len(line) < 30:
            current_section = "results"
            continue
        elif any(x in line_lower for x in ["conclusion", "discussion", "future work"]) and len(line) < 30:
            current_section = "conclusion"
            continue
        elif any(x in line_lower for x in ["reference", "bibliography"]) and len(line) < 30:
            current_section = "references"
            continue
            
        # Add text to current section
        if line and current_section in sections:
            sections[current_section] += line + "\n"
    
    # Clean up sections
    for section in sections:
        if len(sections[section]) > 100000:  # Limit section size
            sections[section] = sections[section][:100000] + "... [truncated]"
    
    return sections

def download_paper_from_url(url, conn, paper_dir, use_mistral_ocr=False):
    """
    Download a paper from a given URL, which can be a direct PDF link or a webpage containing a PDF link
    
    Args:
        url: URL to download the paper from
        conn: Database connection
        paper_dir: Directory to save the paper
        use_mistral_ocr: Whether to use Mistral OCR for text extraction
        
    Returns:
        dict: Paper information or None if download failed
    """
    try:
        # Generate a consistent paper ID based on URL
        paper_id = hashlib.md5(url.encode()).hexdigest()
        
        # Ensure database schema is up to date
        ensure_database_schema(conn)
        
        # Get source ID for the URL
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM source_types WHERE name = 'web'")
        result = cursor.fetchone()
        if result:
            source_id = result[0]
        else:
            # Insert a new source type if it doesn't exist
            cursor.execute("INSERT INTO source_types (name) VALUES ('web')")
            conn.commit()
            source_id = cursor.lastrowid
        
        # Check if paper already exists in database
        cursor.execute("SELECT id FROM research_papers WHERE id = ?", (paper_id,))
        if cursor.fetchone():
            logger.info(f"Paper with ID {paper_id} already exists in database, skipping download")
            return None
            
        # Initialize variables
        pdf_url = url
        title = None
        abstract = ""
        
        # Determine if URL is a direct PDF link or a webpage
        is_pdf_url = url.lower().endswith('.pdf')
        pdf_url = url if is_pdf_url else None
        
        if not is_pdf_url:
            # If not a direct PDF link, try to find PDF link on the webpage
            try:
                response = requests.get(url, headers=HEADERS, timeout=30)
                response.raise_for_status()
                
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # Look for PDF links in the page
                pdf_links = []
                for link in soup.find_all('a', href=True):
                    href = link['href']
                    if href.lower().endswith('.pdf'):
                        # Handle relative URLs
                        if not href.startswith(('http://', 'https://')):
                            base_url = urlparse(url)
                            href = f"{base_url.scheme}://{base_url.netloc}{href if href.startswith('/') else '/' + href}"
                        pdf_links.append(href)
                
                if pdf_links:
                    # Use the first PDF link found
                    pdf_url = pdf_links[0]
                    logger.info(f"Found PDF link on webpage: {pdf_url}")
                else:
                    logger.warning(f"No PDF links found on webpage: {url}")
                    return None
            except Exception as e:
                logger.error(f"Error parsing webpage {url}: {str(e)}")
                return None
        
        if not pdf_url:
            logger.warning(f"Could not determine PDF URL for {url}")
            return None
            
        # Download the PDF
        try:
            response = requests.get(pdf_url, headers=HEADERS, timeout=30)
            response.raise_for_status()
            
            # Ensure content is a PDF
            content_type = response.headers.get('Content-Type', '').lower()
            if 'application/pdf' not in content_type and not pdf_url.lower().endswith('.pdf'):
                logger.warning(f"URL {pdf_url} does not point to a PDF (Content-Type: {content_type})")
                return None
                
            # Save the PDF
            os.makedirs(paper_dir, exist_ok=True)
            pdf_path = os.path.join(paper_dir, f"{paper_id}.pdf")
            
            with open(pdf_path, 'wb') as f:
                f.write(response.content)
                
            logger.info(f"Downloaded PDF from {pdf_url} to {pdf_path}")
            
            # Extract text from PDF
            text = ""
            
            if use_mistral_ocr and mistral_ocr:
                logger.info(f"Using Mistral OCR for text extraction from {pdf_path}")
                try:
                    result = mistral_ocr.extract_text_from_pdf(pdf_path)
                    # Handle tuple return type (text, title)
                    if isinstance(result, tuple) and len(result) > 0:
                        text = result[0]  # Extract just the text part
                        logger.info(f"Successfully extracted text using Mistral OCR")
                    else:
                        text = result
                        logger.info(f"Successfully extracted text using Mistral OCR")
                except Exception as e:
                    logger.error(f"Error extracting text with Mistral OCR: {e}")
                    # Fall back to PyPDF2
                    text = extract_text_with_pypdf2(pdf_path)
                    logger.info(f"Extracted text using PyPDF2 as fallback")
            else:
                logger.info(f"Using PyPDF2 for text extraction from {pdf_path}")
                text = extract_text_with_pypdf2(pdf_path)
            
            # Check for duplicates based on content
            is_duplicate, existing_id = is_duplicate_paper(conn, text, title, abstract)
            if is_duplicate:
                logger.info(f"Paper {paper_id} is a duplicate of existing paper {existing_id}, skipping")
                # Delete the downloaded PDF
                try:
                    os.remove(pdf_path)
                    logger.info(f"Deleted duplicate PDF: {pdf_path}")
                except Exception as e:
                    logger.error(f"Error deleting duplicate PDF: {str(e)}")
                return None
            
            # Use file modification time as publication date
            pub_date = datetime.fromtimestamp(os.path.getmtime(pdf_path)).strftime('%Y-%m-%d')
            
            # Insert paper data into database
            cursor.execute(
                "INSERT INTO research_papers (id, title, pub_date, authors, abstract, full_text, url, pdf_url, source_id, collected_date, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    paper_id,
                    title,
                    pub_date,
                    json.dumps([]),  # No author data
                    abstract,
                    text,
                    url,  # Store the original URL in url field
                    pdf_url,  # Store the actual PDF URL in pdf_url field
                    source_id,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    None,  # No embedding yet
                ),
            )
            conn.commit()
            
            logger.info(f"Added paper {paper_id} to database")
            
            return {
                "id": paper_id,
                "title": title or "Unknown Title",
                "authors": "Unknown",
                "abstract": abstract,
                "pdf_url": pdf_url,
                "pub_date": pub_date,
                "content": text
            }
            
        except Exception as e:
            logger.error(f"Error downloading PDF from {pdf_url}: {str(e)}")
            return None
            
    except Exception as e:
        logger.error(f"Error processing URL {url}: {str(e)}")
        return None

def collect_papers(max_papers=None, force_update=False):
    """
    Collect papers from ArXiv and custom URLs
    
    Args:
        max_papers: Maximum number of papers to collect (None for unlimited)
        force_update: Whether to force update existing papers
        
    Returns:
        int: Number of papers collected
    """
    try:
        # Create directories if they don't exist
        papers_pdf_dir = os.path.join(config.DATA_DIR, "papers", "pdf")
        papers_text_dir = os.path.join(config.DATA_DIR, "papers", "text")
        os.makedirs(papers_pdf_dir, exist_ok=True)
        os.makedirs(papers_text_dir, exist_ok=True)
        
        # Connect to database
        conn = sqlite3.connect(config.DB_PATH)
        
        # Ensure database schema is up to date
        ensure_database_schema(conn)
        
        # Get source type ID for research papers
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM source_types WHERE name = ?",
            ("research_paper",)
        )
        result = cursor.fetchone()
        if not result:
            cursor.execute(
                "INSERT INTO source_types (name, description) VALUES (?, ?)",
                ("research_paper", "Scientific research papers")
            )
            source_type_id = cursor.lastrowid
        else:
            source_type_id = result[0]
            
        conn.commit()
        
        # Initialize counters
        total_papers_added = 0
        
        # Process ArXiv papers if enabled
        if config.ENABLE_ARXIV_COLLECTION:
            logger.info("Starting ArXiv paper collection")
            
            # Get ArXiv papers
            arxiv_papers_added = process_arxiv_papers(
                config.ARXIV_CATEGORIES,
                config.ARXIV_MAX_RESULTS,
                papers_pdf_dir,
                papers_text_dir,
                conn,
                source_type_id,
                force_update
            )
            
            total_papers_added += arxiv_papers_added
            logger.info(f"Added {arxiv_papers_added} papers from ArXiv")
            
            # Check if we've reached the maximum
            if max_papers and total_papers_added >= max_papers:
                logger.info(f"Reached maximum number of papers: {max_papers}")
                conn.close()
                return total_papers_added
        
        # Process custom paper URLs if enabled
        if hasattr(config, 'ENABLE_PAPER_URLS') and config.ENABLE_PAPER_URLS and hasattr(config, 'PAPER_URLS'):
            logger.info("Starting custom paper URL collection")
            
            url_papers_added = 0
            for url in config.PAPER_URLS:
                try:
                    # Check if we've reached the maximum
                    if max_papers and total_papers_added >= max_papers:
                        break
                        
                    # Process the URL
                    paper_info = download_paper_from_url(
                        url, 
                        conn, 
                        papers_pdf_dir,
                        hasattr(config, 'USE_MISTRAL_OCR') and config.USE_MISTRAL_OCR
                    )
                    
                    if paper_info:
                        # Save text to file
                        text_path = os.path.join(papers_text_dir, f"{paper_info['id']}.txt")
                        with open(text_path, 'w', encoding='utf-8') as f:
                            f.write(paper_info['content'])
                            
                        # Add to AI content table
                        try:
                            cursor.execute(
                                """
                                INSERT INTO ai_content (
                                    source_type_id, source_id, title, description, content,
                                    url, date_created, date_collected, metadata, is_indexed
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    source_type_id,
                                    paper_info['id'],
                                    paper_info['title'],
                                    paper_info['abstract'],
                                    paper_info['content'],
                                    paper_info['pdf_url'],
                                    paper_info['pub_date'],
                                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                    json.dumps({
                                        "authors": paper_info['authors'],
                                        "published_date": paper_info['pub_date'],
                                        "year": datetime.strptime(paper_info['pub_date'], '%Y-%m-%d').year,
                                        "source": "custom_url"
                                    }),
                                    0  # Not indexed yet
                                )
                            )
                            conn.commit()
                        except Exception as e:
                            logger.error(f"Error adding paper to ai_content: {str(e)}")
                            
                        url_papers_added += 1
                        total_papers_added += 1
                except Exception as e:
                    logger.error(f"Error processing URL {url}: {str(e)}")
                    continue
                    
            logger.info(f"Added {url_papers_added} papers from custom URLs")
        
        conn.close()
        return total_papers_added
        
    except Exception as e:
        logger.error(f"Error in collect_papers: {str(e)}")
        return 0

def download_arxiv_paper(paper_id, papers_pdf_dir):
    """
    Download a paper from ArXiv by ID
    
    Args:
        paper_id: ArXiv paper ID (e.g., '2104.08653v1')
        papers_pdf_dir: Directory to save the PDF to
    
    Returns:
        str: Path to the downloaded PDF file, or None if failed
    """
    try:
        # Construct URL for the PDF
        pdf_link = f"http://arxiv.org/pdf/{paper_id}"
        
        # Get proxy configuration
        proxies = get_proxy()
        
        # Download the PDF
        pdf_path = os.path.join(papers_pdf_dir, f"{paper_id}.pdf")
        
        # Ensure we don't download the file if it already exists
        if os.path.exists(pdf_path):
            logger.info(f"PDF already exists for {paper_id}, skipping download")
            return pdf_path
        
        # Add a delay to be respectful to ArXiv
        time.sleep(3)
        
        # Download with proxy support
        pdf_response = requests.get(pdf_link, timeout=30, proxies=proxies)
        
        if pdf_response.status_code != 200:
            logger.error(f"Failed to download PDF for {paper_id} from {pdf_link}, status code: {pdf_response.status_code}")
            return None
            
        with open(pdf_path, 'wb') as f:
            f.write(pdf_response.content)
            
        logger.info(f"Downloaded PDF for {paper_id}")
        return pdf_path
        
    except Exception as e:
        logger.error(f"Error downloading paper {paper_id}: {str(e)}")
        return None

def is_duplicate_paper(conn, text, title, abstract, threshold=0.8):
    """
    Check if a paper is a duplicate based on title, abstract, and content similarity
    
    Args:
        conn: Database connection
        text: Full text of the paper
        title: Title of the paper
        abstract: Abstract of the paper
        threshold: Similarity threshold (0-1) for considering a duplicate
        
    Returns:
        tuple: (is_duplicate, existing_id) if duplicate, (False, None) otherwise
    """
    # TEMPORARILY DISABLED - ALWAYS RETURN FALSE FOR TESTING
    return False, None
    
    # Original implementation commented out below
    '''
    try:
        # If text is too short, it's probably not extractable, so skip duplicate check
        if not text or len(text) < 100:
            return False, None
            
        cursor = conn.cursor()
        
        # First try to find duplicates by title similarity
        cursor.execute(
            "SELECT id, title, abstract, content FROM research_papers ORDER BY last_crawled DESC LIMIT 100"
        )
        results = cursor.fetchall()
        
        for row in results:
            existing_id, existing_title, existing_abstract, existing_content = row
            
            # Skip if existing content is empty
            if not existing_content or len(existing_content) < 100:
                continue
                
            # Calculate title similarity using Levenshtein distance
            title_similarity = 0
            if title and existing_title:
                # Normalize strings
                norm_title = title.lower().strip()
                norm_existing_title = existing_title.lower().strip()
                
                # Calculate Levenshtein similarity
                import Levenshtein
                title_similarity = 1 - (Levenshtein.distance(norm_title, norm_existing_title) / 
                                       max(len(norm_title), len(norm_existing_title)))
                
                # If titles are nearly identical, consider it a duplicate
                if title_similarity > 0.9:
                    logger.info(f"Duplicate paper detected by title similarity: {title_similarity:.2f}")
                    return True, existing_id
            
            # Calculate content similarity
            if existing_content and text:
                # Use a simple jaccard similarity on content chunks
                def get_chunks(text, size=5):
                    """Split text into word chunks for comparison"""
                    words = text.lower().split()
                    return set(' '.join(words[i:i+size]) for i in range(max(1, len(words) - size + 1)))
                    
                # Get samples of text for efficiency
                orig_sample = existing_content[:10000] + existing_content[-10000:]
                new_sample = text[:10000] + text[-10000:]
                
                # Calculate Jaccard similarity
                orig_chunks = get_chunks(orig_sample)
                new_chunks = get_chunks(new_sample)
                
                if not orig_chunks or not new_chunks:
                    continue
                    
                intersection = orig_chunks.intersection(new_chunks)
                union = orig_chunks.union(new_chunks)
                
                if not union:
                    continue
                    
                content_similarity = len(intersection) / len(union)
                
                if content_similarity > threshold:
                    logger.info(f"Duplicate paper detected by content similarity: {content_similarity:.2f}")
                    return True, existing_id
        
        return False, None
    except Exception as e:
        logger.error(f"Error checking for duplicate papers: {str(e)}")
        return False, None
    '''

def process_arxiv_papers(categories, max_results, papers_pdf_dir, papers_text_dir, conn, source_type_id, force_update=False):
    """
    Process papers from ArXiv based on categories
    
    Args:
        categories: List of ArXiv categories to search
        max_results: Maximum number of results per category
        papers_pdf_dir: Directory to save PDF files
        papers_text_dir: Directory to save text files
        conn: Database connection
        source_type_id: Source type ID for research papers
        force_update: Whether to force update existing papers
        
    Returns:
        int: Number of papers added
    """
    try:
        papers_added = 0
        
        # Process each category
        for category in categories:
            logger.info(f"Collecting papers for category: {category}")
            
            # Construct ArXiv API query
            query = f"cat:{category}"
            url = f"http://export.arxiv.org/api/query?search_query={query}&sortBy=submittedDate&sortOrder=descending&max_results={max_results}"
            
            try:
                # Get papers from ArXiv
                response = feedparser.parse(url)
                
                if not response.entries:
                    logger.warning(f"No papers found for category: {category}")
                    continue
                    
                logger.info(f"Found {len(response.entries)} papers for category: {category}")
                
                # Process each paper
                for entry in response.entries:
                    try:
                        # Extract paper ID
                        paper_id = entry.id.split('/abs/')[-1] if hasattr(entry, 'id') else f"arxiv_{int(time.time())}"
                        
                        # Check if paper already exists
                        cursor = conn.cursor()
                        cursor.execute("SELECT id FROM research_papers WHERE id = ?", (paper_id,))
                        existing = cursor.fetchone()
                        
                        # Skip if paper exists and not forcing update
                        if existing and not force_update:
                            logger.debug(f"Paper {paper_id} already exists, skipping")
                            continue
                        
                        # Extract publication date
                        published = datetime.now()
                        if hasattr(entry, 'published_parsed') and entry.published_parsed:
                            try:
                                published = datetime(
                                    year=entry.published_parsed[0],
                                    month=entry.published_parsed[1],
                                    day=entry.published_parsed[2]
                                )
                            except Exception as e:
                                logger.warning(f"Error parsing publication date: {str(e)}")
                        
                        # Extract paper details
                        title = entry.get('title', f"Unknown Paper {paper_id}")
                        
                        # Extract authors
                        authors = ""
                        if hasattr(entry, 'authors'):
                            try:
                                authors = ", ".join(author.get('name', '') for author in entry.authors)
                            except Exception as e:
                                logger.warning(f"Error extracting authors: {str(e)}")
                        
                        # Extract abstract
                        abstract = entry.get('summary', "")
                        
                        # Get PDF link
                        pdf_url = None
                        if hasattr(entry, 'links'):
                            for link in entry.links:
                                if hasattr(link, 'title') and link.title == 'pdf':
                                    pdf_url = link.href
                                    break
                        
                        if not pdf_url:
                            pdf_url = f"https://arxiv.org/pdf/{paper_id}.pdf"
                        
                        # Get article URL
                        article_url = f"https://arxiv.org/abs/{paper_id}"
                        
                        # Download PDF
                        pdf_path = os.path.join(papers_pdf_dir, f"{paper_id}.pdf")
                        try:
                            # Download PDF
                            response = requests.get(pdf_url, headers=HEADERS, timeout=30)
                            response.raise_for_status()
                            
                            with open(pdf_path, 'wb') as f:
                                f.write(response.content)
                                
                            logger.info(f"Downloaded PDF for {paper_id}")
                            
                            # Extract text from PDF
                            text = ""
                            
                            # Use Mistral OCR if available
                            if mistral_ocr and hasattr(config, 'USE_MISTRAL_OCR') and config.USE_MISTRAL_OCR:
                                try:
                                    result = mistral_ocr.extract_text_from_pdf(pdf_path)
                                    # Handle tuple return type (text, title)
                                    if isinstance(result, tuple) and len(result) > 0:
                                        text = result[0]  # Extract just the text part
                                        logger.info(f"Successfully extracted text using Mistral OCR")
                                    else:
                                        text = result
                                        logger.info(f"Successfully extracted text using Mistral OCR")
                                except Exception as e:
                                    logger.error(f"Error extracting text with Mistral OCR: {e}")
                                    # Fall back to PyPDF2
                                    text = extract_text_with_pypdf2(pdf_path)
                                    logger.info(f"Extracted text using PyPDF2 as fallback")
                            else:
                                text = extract_text_with_pypdf2(pdf_path)
                                logger.info(f"Extracted text using PyPDF2")
                            
                            # Save text to file
                            if text:
                                text_path = os.path.join(papers_text_dir, f"{paper_id}.txt")
                                with open(text_path, 'w', encoding='utf-8') as f:
                                    f.write(text)
                            
                            # Check for duplicates
                            is_duplicate, existing_id = is_duplicate_paper(conn, text, title, abstract)
                            if is_duplicate:
                                logger.info(f"Paper {paper_id} is a duplicate of {existing_id}, skipping")
                                # Delete the downloaded PDF
                                try:
                                    os.remove(pdf_path)
                                except Exception as e:
                                    logger.error(f"Error deleting duplicate PDF: {str(e)}")
                                continue
                            
                            # Insert or update paper in database
                            if existing:
                                # Update existing paper
                                cursor.execute(
                                    """
                                    UPDATE research_papers 
                                    SET title = ?, authors = ?, abstract = ?, pdf_url = ?, pub_date = ?, content = ?, last_crawled = ?
                                    WHERE id = ?
                                    """,
                                    (
                                        title,
                                        authors,
                                        abstract,
                                        pdf_url,
                                        published.strftime('%Y-%m-%d'),
                                        text,
                                        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                        paper_id
                                    )
                                )
                                logger.info(f"Updated paper {paper_id} in database")
                            else:
                                # Insert new paper
                                cursor.execute(
                                    """
                                    INSERT INTO research_papers (id, title, authors, abstract, pdf_url, pub_date, content, last_crawled)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        paper_id,
                                        title,
                                        authors,
                                        abstract,
                                        pdf_url,
                                        published.strftime('%Y-%m-%d'),
                                        text,
                                        datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                    )
                                )
                                logger.info(f"Added paper {paper_id} to database")
                                papers_added += 1
                            
                            # Add to AI content table
                            try:
                                # Check if already exists in ai_content
                                cursor.execute(
                                    "SELECT id FROM ai_content WHERE source_type_id = ? AND source_id = ?",
                                    (source_type_id, paper_id)
                                )
                                ai_content_exists = cursor.fetchone()
                                
                                if ai_content_exists:
                                    # Update existing record
                                    cursor.execute(
                                        """
                                        UPDATE ai_content
                                        SET title = ?, description = ?, content = ?, url = ?, date_created = ?, date_collected = ?, metadata = ?
                                        WHERE source_type_id = ? AND source_id = ?
                                        """,
                                        (
                                            title,
                                            abstract,
                                            text,
                                            article_url,
                                            published.strftime('%Y-%m-%d'),
                                            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                            json.dumps({
                                                "authors": authors,
                                                "published_date": published.strftime('%Y-%m-%d'),
                                                "year": published.year,
                                                "source": "arxiv",
                                                "category": category
                                            }),
                                            source_type_id,
                                            paper_id
                                        )
                                    )
                                else:
                                    # Insert new record
                                    cursor.execute(
                                        """
                                        INSERT INTO ai_content (source_type_id, source_id, title, description, content, url, date_created, date_collected, metadata, is_indexed)
                                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                        """,
                                        (
                                            source_type_id,
                                            paper_id,
                                            title,
                                            abstract,
                                            text,
                                            article_url,
                                            published.strftime('%Y-%m-%d'),
                                            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                            json.dumps({
                                                "authors": authors,
                                                "published_date": published.strftime('%Y-%m-%d'),
                                                "year": published.year,
                                                "source": "arxiv",
                                                "category": category
                                            }),
                                            0  # Not indexed yet
                                        )
                                    )
                            except Exception as e:
                                logger.error(f"Error adding paper to ai_content: {str(e)}")
                            
                            conn.commit()
                            
                        except Exception as e:
                            logger.error(f"Error downloading PDF for {paper_id}: {str(e)}")
                            continue
                            
                    except Exception as e:
                        logger.error(f"Error processing paper entry: {str(e)}")
                        continue
                    
                    # Add a delay to be respectful of the API
                    time.sleep(3)
                    
            except Exception as e:
                logger.error(f"Error collecting papers for category {category}: {str(e)}")
                continue
                
        return papers_added
        
    except Exception as e:
        logger.error(f"Error in process_arxiv_papers: {str(e)}")
        return 0

def ensure_database_schema(conn):
    """
    Ensure that the database schema has all required tables and columns
    
    Args:
        conn: Database connection
    """
    cursor = conn.cursor()
    try:
        # Check if research_papers table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='research_papers'")
        if not cursor.fetchone():
            logger.info("Creating research_papers table")
            cursor.execute("""
                CREATE TABLE research_papers (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    pub_date TEXT,
                    authors TEXT,
                    abstract TEXT,
                    full_text TEXT,
                    url TEXT,
                    pdf_url TEXT,
                    source_id INTEGER,
                    collected_date TEXT,
                    embedding BLOB
                )
            """)
            conn.commit()
        else:
            # Check if pdf_url column exists
            cursor.execute("PRAGMA table_info(research_papers)")
            columns = [column[1] for column in cursor.fetchall()]
            
            if "pdf_url" not in columns:
                logger.info("Adding pdf_url column to research_papers table")
                cursor.execute("ALTER TABLE research_papers ADD COLUMN pdf_url TEXT")
                conn.commit()
                
        # Check if source_types table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='source_types'")
        if not cursor.fetchone():
            logger.info("Creating source_types table")
            cursor.execute("""
                CREATE TABLE source_types (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE
                )
            """)
            # Insert default source types
            cursor.execute("INSERT INTO source_types (name) VALUES ('arxiv')")
            cursor.execute("INSERT INTO source_types (name) VALUES ('web')")
            conn.commit()
            
    except Exception as e:
        logger.error(f"Error ensuring database schema: {e}")
        conn.rollback()

def download_papers_only(max_papers=50, categories=None, save_dir=None, custom_search_query=None):
    """
    Download papers from ArXiv without processing them.
    This function only downloads the PDFs and stores their metadata in a temporary file.
    
    Args:
        max_papers: Maximum number of papers to download
        categories: List of ArXiv categories to search for
        save_dir: Directory to save PDFs to
        custom_search_query: Custom search query to use instead of the one in config
    """
    if categories is None:
        categories = config.ARXIV_CATEGORIES
    
    if save_dir is None:
        save_dir = config.PAPERS_DIR
        
    # Ensure directories exist
    os.makedirs(save_dir, exist_ok=True)
    pdf_dir = os.path.join(save_dir, "pdf")
    os.makedirs(pdf_dir, exist_ok=True)
    
    # Create a temporary metadata file to store paper information
    metadata_file = os.path.join(save_dir, "pending_papers.json")
    pending_papers = []
    
    if os.path.exists(metadata_file):
        try:
            with open(metadata_file, 'r') as f:
                pending_papers = json.load(f)
            logger.info(f"Loaded {len(pending_papers)} pending papers from {metadata_file}")
        except Exception as e:
            logger.error(f"Error loading pending papers: {e}")
    
    # Connect to database to check for existing papers
    conn = sqlite3.connect(config.DB_PATH)
    ensure_database_schema(conn)
    cursor = conn.cursor()
    
    logger.info(f"Downloading up to {max_papers} papers from categories: {categories}")
    
    # Query ArXiv
    search_query = ""
    
    # Use the custom search query if provided
    if custom_search_query:
        search_query = custom_search_query
        logger.info(f"Using custom search query: {search_query}")
        # Combine with categories if specified
        if categories:
            search_query = f"({search_query}) AND ({' OR '.join(f'cat:{category}' for category in categories)})"
    # Otherwise use the search query from config if available
    elif hasattr(config, 'ARXIV_CONFIG') and 'search_query' in config.ARXIV_CONFIG:
        search_query = config.ARXIV_CONFIG['search_query']
        logger.info(f"Using search query from config: {search_query}")
        # Combine with categories
        if categories:
            search_query = f"({search_query}) AND ({' OR '.join(f'cat:{category}' for category in categories)})"
    else:
        # Otherwise just use categories
        search_query = ' OR '.join(f'cat:{category}' for category in categories)
    
    search = arxiv.Search(
        query=search_query,
        max_results=max_papers,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending
    )
    
    # Get the results - this makes it iterable
    client = arxiv.Client()
    papers = client.results(search)
    
    download_count = 0
    
    for paper in papers:
        paper_id = paper.get_short_id()
        title = paper.title
        
        # Check if we already have this paper in pending list
        if any(p.get('arxiv_id') == paper_id for p in pending_papers):
            logger.info(f"Paper {paper_id} - '{title}' already in pending list, skipping")
            continue
        
        # Check if paper already exists in database
        cursor.execute("SELECT id FROM research_papers WHERE id = ?", (paper_id,))
        if cursor.fetchone():
            logger.info(f"Paper {paper_id} - '{title}' already exists in database, skipping")
            continue
        
        try:
            # Create a clean filename
            pdf_filename = f"{paper_id}.pdf"
            pdf_path = os.path.join(pdf_dir, pdf_filename)
            logger.info(f"Downloading paper {paper_id} - '{title}'")
            
            # Download PDF directly using requests
            pdf_url = paper.pdf_url
            response = requests.get(pdf_url, stream=True)
            response.raise_for_status()  # Raise an error for bad responses
            
            with open(pdf_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            logger.info(f"Downloaded PDF to {pdf_path}")
            
            # Save metadata
            paper_info = {
                'arxiv_id': paper_id,
                'title': title,
                'authors': [author.name for author in paper.authors],
                'summary': paper.summary,
                'date': paper.published.strftime('%Y-%m-%d'),
                'categories': paper.categories,
                'pdf_url': paper.pdf_url,
                'pdf_path': pdf_path
            }
            
            pending_papers.append(paper_info)
            download_count += 1
            
            # Periodically save the pending papers list
            if download_count % 5 == 0:
                with open(metadata_file, 'w') as f:
                    json.dump(pending_papers, f, indent=2)
                    
        except Exception as e:
            logger.error(f"Error downloading paper {paper_id}: {e}")
    
    # Save the final list of pending papers
    with open(metadata_file, 'w') as f:
        json.dump(pending_papers, f, indent=2)
    
    conn.close()
    logger.info(f"Downloaded {download_count} new papers. Total pending papers: {len(pending_papers)}")
    return download_count

def batch_process_pdfs(max_papers=None, use_mistral=True):
    """
    Process PDFs in batches, extracting text and inserting into the database.
    
    Args:
        max_papers: Maximum number of papers to process in this batch
        use_mistral: Whether to use Mistral OCR for text extraction
    """
    # Load pending papers
    pending_papers_file = os.path.join(config.PAPERS_DIR, "pending_papers.json")
    if not os.path.exists(pending_papers_file):
        logger.info("No pending papers found.")
        return 0
    
    with open(pending_papers_file, 'r') as f:
        pending_papers = json.load(f)
    
    logger.info(f"Loaded {len(pending_papers)} pending papers")
    
    if max_papers is not None:
        pending_papers = pending_papers[:max_papers]
        logger.info(f"Processing up to {max_papers} papers")
    
    # Connect to database
    conn = sqlite3.connect(config.DB_PATH)
    ensure_database_schema(conn)
    cursor = conn.cursor()
    
    # Initialize Mistral OCR if needed
    mistral_ocr = None
    if use_mistral and hasattr(config, 'MISTRAL_API_KEY'):
        try:
            from mistral_ocr import MistralOCR
            mistral_ocr = MistralOCR(api_key=config.MISTRAL_API_KEY)
            logger.info("Initialized Mistral OCR")
        except Exception as e:
            logger.error(f"Failed to initialize Mistral OCR: {e}")
            use_mistral = False
    else:
        use_mistral = False
        logger.info("Using PyPDF2 for text extraction")
    
    processed_count = 0
    remaining_papers = []
    
    for paper in pending_papers:
        paper_id = paper.get('arxiv_id')
        title = paper.get('title')
        pdf_path = paper.get('pdf_path')
        
        if not os.path.exists(pdf_path):
            logger.warning(f"PDF file not found: {pdf_path}, skipping")
            continue
        
        # Check if paper already exists in database
        cursor.execute("SELECT id FROM research_papers WHERE id = ?", (paper_id,))
        if cursor.fetchone():
            logger.info(f"Paper {paper_id} - '{title}' already exists in database, skipping")
            # Try to delete the PDF to save space
            try:
                os.remove(pdf_path)
                logger.info(f"Deleted duplicate PDF: {pdf_path}")
            except Exception as e:
                logger.warning(f"Failed to delete duplicate PDF: {e}")
            continue
        
        logger.info(f"Processing paper {paper_id} - '{title}'")
        
        try:
            # Extract text from PDF
            extracted_text = ""
            
            if use_mistral and mistral_ocr:
                try:
                    result = mistral_ocr.extract_text_from_pdf(pdf_path)
                    # Handle tuple return type (text, title)
                    if isinstance(result, tuple) and len(result) > 0:
                        extracted_text = result[0]  # Extract just the text part
                        logger.info(f"Successfully extracted text using Mistral OCR")
                    else:
                        extracted_text = result
                        logger.info(f"Successfully extracted text using Mistral OCR")
                except Exception as e:
                    logger.error(f"Error extracting text with Mistral OCR: {e}")
                    # Fall back to PyPDF2
                    extracted_text = extract_text_from_pdf(pdf_path)
                    logger.info(f"Extracted text using PyPDF2 as fallback")
            else:
                extracted_text = extract_text_from_pdf(pdf_path)
                logger.info(f"Extracted text using PyPDF2")
            
            # Insert paper into database
            try:
                # Map ArXiv metadata to database schema
                authors = ", ".join(paper.get('authors', []))
                abstract = paper.get('summary', '')
                publication = "arXiv"
                year = int(paper.get('date', '').split('-')[0]) if paper.get('date') else None
                url = f"https://arxiv.org/abs/{paper_id}"
                pdf_url = paper.get('pdf_url', '')
                
                # Insert into database - using ArXiv ID as the DOI since ID is INTEGER PRIMARY KEY
                cursor.execute("""
                    INSERT INTO research_papers 
                    (title, authors, abstract, publication, year, url, doi, pdf_path, content, pdf_url) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    title, authors, abstract, publication, 
                    year, url, paper_id, pdf_path, extracted_text, pdf_url
                ))
                conn.commit()
                logger.info(f"Inserted paper {paper_id} into database")
                processed_count += 1
                
            except Exception as e:
                logger.error(f"Error inserting paper into database: {e}")
                remaining_papers.append(paper)
                
        except Exception as e:
            logger.error(f"Error processing paper {paper_id}: {e}")
            remaining_papers.append(paper)
    
    # Update pending papers file with remaining papers
    with open(pending_papers_file, 'w') as f:
        json.dump(remaining_papers, f, indent=2)
    
    conn.close()
    logger.info(f"Processed {processed_count} papers. {len(remaining_papers)} papers remaining.")
    return processed_count

# Add main block for direct invocation
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ArXiv paper collector and processor")
    parser.add_argument("--collect", action="store_true", help="Collect papers from ArXiv")
    parser.add_argument("--download-only", action="store_true", help="Only download papers without processing")
    parser.add_argument("--batch-process", action="store_true", help="Process previously downloaded PDFs")
    parser.add_argument("--max", type=int, default=50, help="Maximum number of papers to process")
    parser.add_argument("--url", type=str, help="Download and process a single paper from URL")
    parser.add_argument("--urls-file", type=str, help="Path to a text file containing paper URLs (one per line)")
    parser.add_argument("--download-url", type=str, help="Download a single paper from URL without processing")
    parser.add_argument("--download-urls-file", type=str, help="Path to a text file containing paper URLs to download without processing")
    parser.add_argument("--search-query", type=str, help="Custom search query for ArXiv (overrides config)")
    
    args = parser.parse_args()
    
    # Setup directories
    papers_dir = config.PAPERS_DIR
    os.makedirs(papers_dir, exist_ok=True)
    pdf_dir = os.path.join(papers_dir, "pdf")
    os.makedirs(pdf_dir, exist_ok=True)
    
    # Connect to database for URL downloads
    if args.url or args.urls_file or args.download_url or args.download_urls_file:
        conn = sqlite3.connect(config.DB_PATH)
        ensure_database_schema(conn)
    
    if args.url:
        # Download and process a single paper
        use_mistral = hasattr(config, 'USE_MISTRAL_OCR') and config.USE_MISTRAL_OCR
        paper_info = download_paper_from_url(args.url, conn, pdf_dir, use_mistral)
        if paper_info:
            logger.info(f"Successfully downloaded and processed paper: {paper_info.get('title', 'Unknown')}")
        else:
            logger.error("Failed to download or process the paper")
    
    elif args.urls_file:
        # Download and process multiple papers from a file
        try:
            with open(args.urls_file, 'r') as f:
                urls = [line.strip() for line in f if line.strip()]
            
            logger.info(f"Found {len(urls)} URLs in file")
            use_mistral = hasattr(config, 'USE_MISTRAL_OCR') and config.USE_MISTRAL_OCR
            
            success_count = 0
            for url in urls:
                try:
                    paper_info = download_paper_from_url(url, conn, pdf_dir, use_mistral)
                    if paper_info:
                        success_count += 1
                        logger.info(f"Successfully downloaded and processed paper {success_count}/{len(urls)}: {paper_info.get('title', 'Unknown')}")
                    else:
                        logger.error(f"Failed to download or process paper: {url}")
                except Exception as e:
                    logger.error(f"Error processing URL {url}: {str(e)}")
            
            logger.info(f"Completed processing {success_count}/{len(urls)} papers")
        except Exception as e:
            logger.error(f"Error processing URLs file: {str(e)}")
    
    elif args.download_url:
        # Download a single paper to pending list
        try:
            # Use arxiv_id from URL if it's an arXiv URL
            arxiv_id = None
            if "arxiv.org" in args.download_url:
                # Extract arxiv ID from URL patterns like:
                # https://arxiv.org/abs/1234.56789
                # https://arxiv.org/pdf/1234.56789.pdf
                url_parts = args.download_url.split('/')
                for part in url_parts:
                    if '.' in part and not part.endswith('.pdf'):
                        potential_id = part.split('v')[0] if 'v' in part else part
                        if re.match(r'^\d+\.\d+$', potential_id):
                            arxiv_id = part
                            break
            
            if arxiv_id:
                logger.info(f"Detected arXiv ID: {arxiv_id}")
                # Download using arxiv API if possible
                if ARXIV_AVAILABLE:
                    try:
                        search = arxiv.Search(id_list=[arxiv_id])
                        client = arxiv.Client()
                        results = list(client.results(search))
                        
                        if results:
                            paper = results[0]
                            paper_id = paper.get_short_id()
                            title = paper.title
                            
                            # Create the metadata file path
                            metadata_file = os.path.join(papers_dir, "pending_papers.json")
                            pending_papers = []
                            
                            if os.path.exists(metadata_file):
                                try:
                                    with open(metadata_file, 'r') as f:
                                        pending_papers = json.load(f)
                                except Exception as e:
                                    logger.error(f"Error loading pending papers: {e}")
                            
                            # Check if paper is already in pending list
                            if any(p.get('arxiv_id') == paper_id for p in pending_papers):
                                logger.info(f"Paper {paper_id} already in pending list")
                            else:
                                # Download PDF
                                pdf_filename = f"{paper_id}.pdf"
                                pdf_path = os.path.join(pdf_dir, pdf_filename)
                                
                                # Download PDF directly using requests
                                pdf_url = paper.pdf_url
                                response = requests.get(pdf_url, stream=True)
                                response.raise_for_status()
                                
                                with open(pdf_path, 'wb') as f:
                                    for chunk in response.iter_content(chunk_size=8192):
                                        f.write(chunk)
                                
                                logger.info(f"Downloaded PDF to {pdf_path}")
                                
                                # Save metadata
                                paper_info = {
                                    'arxiv_id': paper_id,
                                    'title': title,
                                    'authors': [author.name for author in paper.authors],
                                    'summary': paper.summary,
                                    'date': paper.published.strftime('%Y-%m-%d'),
                                    'categories': paper.categories,
                                    'pdf_url': paper.pdf_url,
                                    'pdf_path': pdf_path
                                }
                                
                                pending_papers.append(paper_info)
                                
                                # Save pending papers
                                with open(metadata_file, 'w') as f:
                                    json.dump(pending_papers, f, indent=2)
                                
                                logger.info(f"Added paper to pending list: {title}")
                                logger.info(f"Total pending papers: {len(pending_papers)}")
                        else:
                            logger.error(f"No papers found with ID {arxiv_id}")
                    except Exception as e:
                        logger.error(f"Error downloading paper with arXiv ID {arxiv_id}: {str(e)}")
                else:
                    logger.error("ArXiv module not available")
            else:
                logger.error("Not an arXiv URL or couldn't extract arXiv ID")
        except Exception as e:
            logger.error(f"Error downloading paper from URL: {str(e)}")
    
    elif args.download_urls_file:
        # Download multiple papers from URLs file to pending list
        try:
            with open(args.download_urls_file, 'r') as f:
                urls = [line.strip() for line in f if line.strip()]
            
            logger.info(f"Found {len(urls)} URLs in file")
            
            # Create the metadata file path
            metadata_file = os.path.join(papers_dir, "pending_papers.json")
            pending_papers = []
            
            if os.path.exists(metadata_file):
                try:
                    with open(metadata_file, 'r') as f:
                        pending_papers = json.load(f)
                except Exception as e:
                    logger.error(f"Error loading pending papers: {e}")
            
            success_count = 0
            for url in urls:
                try:
                    # Process ArXiv URLs
                    if "arxiv.org" in url:
                        # Extract arxiv ID from URL
                        arxiv_id = None
                        url_parts = url.split('/')
                        for part in url_parts:
                            if '.' in part and not part.endswith('.pdf'):
                                potential_id = part.split('v')[0] if 'v' in part else part
                                if re.match(r'^\d+\.\d+$', potential_id):
                                    arxiv_id = part
                                    break
                        
                        if not arxiv_id:
                            logger.error(f"Could not extract ArXiv ID from URL: {url}")
                            continue
                        
                        logger.info(f"Detected arXiv ID: {arxiv_id}")
                        
                        # Check if arxiv module is available
                        if not ARXIV_AVAILABLE:
                            logger.error("ArXiv module not available")
                            continue
                        
                        # Download using arxiv API
                        search = arxiv.Search(id_list=[arxiv_id])
                        client = arxiv.Client()
                        results = list(client.results(search))
                        
                        if not results:
                            logger.error(f"No papers found with ID {arxiv_id}")
                            continue
                        
                        paper = results[0]
                        paper_id = paper.get_short_id()
                        title = paper.title
                        
                        # Check if paper is already in pending list
                        if any(p.get('arxiv_id') == paper_id for p in pending_papers):
                            logger.info(f"Paper {paper_id} already in pending list")
                            continue
                        
                        # Check if paper already exists in database
                        cursor = conn.cursor()
                        cursor.execute("SELECT id FROM research_papers WHERE id = ?", (paper_id,))
                        if cursor.fetchone():
                            logger.info(f"Paper {paper_id} - '{title}' already exists in database, skipping")
                            continue
                        
                        # Download PDF
                        pdf_filename = f"{paper_id}.pdf"
                        pdf_path = os.path.join(pdf_dir, pdf_filename)
                        
                        # Download PDF directly using requests
                        pdf_url = paper.pdf_url
                        response = requests.get(pdf_url, stream=True)
                        response.raise_for_status()
                        
                        with open(pdf_path, 'wb') as f:
                            for chunk in response.iter_content(chunk_size=8192):
                                f.write(chunk)
                        
                        logger.info(f"Downloaded PDF to {pdf_path}")
                        
                        # Save metadata
                        paper_info = {
                            'arxiv_id': paper_id,
                            'title': title,
                            'authors': [author.name for author in paper.authors],
                            'summary': paper.summary,
                            'date': paper.published.strftime('%Y-%m-%d'),
                            'categories': paper.categories,
                            'pdf_url': paper.pdf_url,
                            'pdf_path': pdf_path
                        }
                        
                        pending_papers.append(paper_info)
                        success_count += 1
                        logger.info(f"Added paper to pending list: {title}")
                        
                    else:
                        # Handle non-ArXiv URLs with the download_paper_from_url function
                        # This just downloads to the database, not to pending_papers.json
                        use_mistral = hasattr(config, 'USE_MISTRAL_OCR') and config.USE_MISTRAL_OCR
                        paper_info = download_paper_from_url(url, conn, pdf_dir, use_mistral)
                        if paper_info:
                            success_count += 1
                            logger.info(f"Successfully downloaded and processed non-ArXiv paper: {paper_info.get('title', 'Unknown')}")
                        else:
                            logger.error(f"Failed to download or process non-ArXiv paper: {url}")
                    
                except Exception as e:
                    logger.error(f"Error processing URL {url}: {str(e)}")
            
            # Save pending papers
            with open(metadata_file, 'w') as f:
                json.dump(pending_papers, f, indent=2)
                
            logger.info(f"Completed downloading {success_count}/{len(urls)} papers")
            logger.info(f"Total pending papers: {len(pending_papers)}")
            
        except Exception as e:
            logger.error(f"Error processing URLs file: {str(e)}")
    
    elif args.download_only:
        # Override search query if provided
        custom_search = None
        if args.search_query:
            logger.info(f"Using custom search query: {args.search_query}")
            custom_search = args.search_query
            # Also set in config for other functions that might use it
            if not hasattr(config, 'ARXIV_CONFIG'):
                config.ARXIV_CONFIG = {}
            config.ARXIV_CONFIG['search_query'] = args.search_query
        
        download_papers_only(max_papers=args.max, custom_search_query=custom_search)
    
    elif args.batch_process:
        batch_process_pdfs(max_papers=args.max)
    
    elif args.collect:
        collect_papers(max_papers=args.max)
    
    else:
        parser.print_help()
    
    # Close database connection if opened
    if args.url or args.urls_file or args.download_url or args.download_urls_file:
        conn.close() 