"""
================================================================================
  FLAZIO IMPORT ASSISTANT - V15
  Senior Python Developer & Automation Architect Edition
================================================================================
  Descrizione:
    Script Playwright-based per la navigazione completa di un sito web,
    estrazione di risorse (font, immagini, documenti, testi, prodotti)
    e generazione di un CSV 100% compatibile con il sistema Flazio.

  Struttura cartelle generata:
    Imported_Sites/
    └── [Nome_Sito]/
        ├── Fonts/
        │   └── [FamigliaFont]/
        │       └── NomeFont.ttf
        ├── Images/
        │   └── [NomePagina]/
        │       └── immagine.jpg
        ├── Documents/
        │   └── documento.pdf
        ├── Text/
        │   └── [NomePagina].txt
        └── products_[Nome_Sito].csv

  Schema CSV obbligatorio (Flazio):
    MODEL, ID, REF, CODE, TYPE, NAME, DESCRIPTION, STATUS, VAT,
    OPTIONS, PRICE, QUANTITY, WEIGHT, CATEGORIES, TAGS, IMAGE, BRAND

  Dipendenze:
    pip install playwright beautifulsoup4 requests fonttools[woff] brotli
    playwright install chromium
================================================================================
"""

import os
import re
import csv
import json
import sys
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse, urljoin, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed


# ---------------------------------------------------------------------------
# AUTO-RIPARAZIONE DIPENDENZE (utile in ambienti VS Code con venv separati)
# ---------------------------------------------------------------------------
def _ensure_dependencies():
    """Verifica e installa automaticamente le dipendenze mancanti."""
    required = {
        "playwright": "playwright",
        "bs4": "beautifulsoup4",
        "requests": "requests",
        "fontTools": "fonttools[woff]",
        "brotli": "brotli",
    }
    for module, package in required.items():
        try:
            __import__(module)
        except ImportError:
            print(f"📦 Installazione dipendenza mancante: {package}")
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--quiet", package]
            )

_ensure_dependencies()

# Forza stdout/stderr in UTF-8 con sostituzione sicura dei caratteri non encodabili.
# Necessario su macOS con Python 3.9 per evitare UnicodeEncodeError con le emoji
# che vengono a volte salvate come surrogate pairs nei file sorgente.
import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(_sys.stderr, "reconfigure"):
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Import delle dipendenze principali (dopo la verifica)
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page


# ---------------------------------------------------------------------------
# CONFIGURAZIONE LOGGING
# ---------------------------------------------------------------------------
import logging
import traceback

logger = logging.getLogger("FlazioImportAssistant")
logger.setLevel(logging.DEBUG)

def _setup_global_logging():
    """Imposta il logging globale per registrare gli errori non gestiti e generali."""
    root_imported = Path(__file__).parent / "Imported_Sites"
    root_imported.mkdir(exist_ok=True)
    global_log_path = root_imported / "global_import_details.txt"
    
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] (Thread: %(threadName)s) %(message)s"
    )
    
    try:
        file_handler = logging.FileHandler(global_log_path, encoding="utf-8")
        file_handler.setLevel(logging.WARNING)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception as e:
        print(f"⚠️ Impossibile configurare il file log globale: {e}", file=sys.stderr)
        
    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.critical(
            "Errore non gestito che ha causato l'arresto dello script:",
            exc_info=(exc_type, exc_value, exc_traceback)
        )
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        
    sys.excepthook = handle_exception

_setup_global_logging()



# ---------------------------------------------------------------------------
# COSTANTI
# ---------------------------------------------------------------------------

# Schema CSV obbligatorio Flazio — NON modificare l'ordine o i nomi
FLAZIO_CSV_HEADERS = [
    "MODEL", "ID", "REF", "CODE", "TYPE", "NAME", "DESCRIPTION",
    "STATUS", "VAT", "OPTIONS", "PRICE", "QUANTITY", "WEIGHT",
    "CATEGORIES", "TAGS", "IMAGE", "BRAND",
]

# Estensioni documenti scaricabili
DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")

# Estensioni font supportati
FONT_EXTENSIONS = (".woff", ".woff2", ".ttf", ".otf")

# Estensioni immagini supportate
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg")

# Sottostringhe nei nomi font da escludere (alfabeti non latini / varianti inutili)
FONT_BLACKLIST = [
    "cyrillic", "greek", "vietnamese", "arabic", "hebrew",
    "thai", "devanagari", "math", "-ext",
]

# Numero massimo di pagine da scansionare (sicurezza anti-loop)
MAX_PAGES = 60

# Pagine processate in parallelo (3 è il trade-off ottimale: velocità vs stabilità)
CONCURRENT_PAGES = 3

# Worker thread per i download delle immagini
DOWNLOAD_WORKERS = 15

# Tipi di risorse da bloccare nel browser (non necessari per lo scraping)
BLOCKED_RESOURCE_TYPES = {"media", "websocket", "eventsource", "manifest"}

# Pattern di URL da bloccare (analytics, ads, tracking)
BLOCKED_URL_PATTERNS = [
    "*google-analytics*", "*googletagmanager*", "*facebook.net*",
    "*doubleclick*", "*hotjar*", "*intercom*", "*crisp.chat*",
    "*analytics.js*", "*gtag/js*", "*fbevents*", "*clarity.ms*",
]


# ---------------------------------------------------------------------------
# CLASSE PRINCIPALE
# ---------------------------------------------------------------------------

class FlazioImportAssistant:
    """
    Crawler Playwright + BeautifulSoup per l'importazione di siti web su Flazio.

    Flusso principale:
      1. Carica la sitemap XML (se disponibile) per un elenco iniziale di URL.
      2. Naviga sequenzialmente ogni pagina con un unico browser Playwright.
      3. Per ogni pagina: estrae link interni, font, documenti, testi, immagini,
         e rileva eventuali prodotti.
      4. Al termine genera il CSV compatibile Flazio.
    """

    def __init__(self, target_url: str):
        # Normalizzazione URL di input
        if not target_url.startswith(("http://", "https://")):
            target_url = "https://" + target_url

        # Controllo DNS intelligente per gestire host www vs non-www
        import socket
        parsed = urlparse(target_url)
        netloc = parsed.netloc
        hostname = netloc.split(":")[0]

        def _resolves(host: str) -> bool:
            try:
                socket.gethostbyname(host)
                return True
            except Exception:
                return False

        if not _resolves(hostname):
            alt_host = hostname[4:] if hostname.startswith("www.") else "www." + hostname
            if _resolves(alt_host):
                new_netloc = alt_host
                if ":" in netloc:
                    new_netloc = f"{alt_host}:{netloc.split(':')[1]}"
                parsed = parsed._replace(netloc=new_netloc)
                target_url = parsed.geturl()
                print(f"   ℹ️  Rilevato problema DNS per '{hostname}'. Autocorretto in: {target_url}")

        self.target_url: str = target_url

        # Estrae dominio pulito (senza "www.")
        self.domain: str = parsed.netloc.replace("www.", "")
        self.site_name: str = self._sanitize_name(self.domain.split(".")[0])


        # --- Struttura cartelle (pathlib) ---
        # Root: Imported_Sites/[Nome_Sito]/
        self.root_dir: Path = (
            Path(__file__).parent / "Imported_Sites" / self._sanitize_name(self.domain)
        )
        self.fonts_dir: Path    = self.root_dir / "Fonts"
        self.images_dir: Path   = self.root_dir / "Images"
        self.documents_dir: Path = self.root_dir / "Documents"
        self.text_dir: Path     = self.root_dir / "Text"
        self.csv_path: Path     = self.root_dir / f"products_{self.site_name}.csv"

        # --- Stato del crawler ---
        self.pages_to_crawl: set = {self.target_url}  # URL in coda
        self.visited_urls: set   = set()               # URL già visitati (anti-ciclo)
        self.scraped_css: set    = set()               # CSS già processati
        self.downloaded_font_keys: set = set()         # Font già scaricati (anti-dup)
        self.detected_products: list   = []            # Prodotti rilevati per il CSV

        # --- Thread safety per elaborazione parallela ---
        self._lock = threading.Lock()  # Protegge le strutture condivise
        # Cache globale URL file già scaricati (mappa: url -> percorso_file)
        self.downloaded_files: dict = {}

        # --- Statistiche scraping (per il riepilogo finale) ---
        self.stats = {
            "pages_ok": 0,          # Pagine caricate con successo
            "pages_failed": 0,      # Pagine non caricabili
            "images_downloaded": 0,  # Immagini scaricate (Playwright + HTTP)
            "images_failed": 0,     # Immagini con 404 / errore
            "images_cached": 0,     # Immagini già in cache (skip)
            "fonts_converted": 0,   # Font convertiti in TTF/OTF
            "fonts_duplicated": 0,  # Font duplicati ignorati
            "documents_downloaded": 0,  # Documenti (PDF, DOC, ecc.)
            "texts_saved": 0,       # File testo salvati
            "products_detected": 0, # Prodotti rilevati
            "pages_detail": [],     # Dettaglio per pagina: (nome, immagini, doc, ...)
        }

        # Riferimenti Playwright (inizializzati in run())
        self._playwright = None
        self._browser: Browser = None

    # -----------------------------------------------------------------------
    # SETUP
    # -----------------------------------------------------------------------

    def _setup_directories(self) -> None:
        """Crea la cartella principale se non esiste già."""
        self.root_dir.mkdir(parents=True, exist_ok=True)

        # Rimuove eventuali site-handler precedenti per evitare doppioni
        if hasattr(self, '_site_handler') and self._site_handler in logger.handlers:
            logger.removeHandler(self._site_handler)
            self._site_handler.close()

        # Configura il logger specifico per questo sito
        site_log_path = self.root_dir / "import_details.txt"
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] (Thread: %(threadName)s) %(message)s"
        )
        try:
            site_handler = logging.FileHandler(site_log_path, encoding="utf-8")
            site_handler.setLevel(logging.WARNING)
            site_handler.setFormatter(formatter)
            logger.addHandler(site_handler)
            self._site_handler = site_handler
        except Exception as e:
            print(f"   ⚠️  Impossibile configurare il file log per il sito: {e}")


    # -----------------------------------------------------------------------
    # UTILITY: Sanitizzazione e naming
    # -----------------------------------------------------------------------

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Rimuove i caratteri vietati nei file system (regex) e normalizza."""
        name = re.sub(r'[\\/*?:"<>|]', "", name)
        name = re.sub(r"\s+", "_", name.strip())
        return name or "unnamed"

    def _get_page_name(self, url: str) -> str:
        """Ricava un nome-file leggibile dall'URL della pagina."""
        path = urlparse(url).path
        if path in ("", "/", "/index.html", "/index.php", "/index.asp"):
            return "home"
        name = path.strip("/").replace("/", "-")
        name = re.sub(r"\.(html|php|asp|aspx|htm)$", "", name, flags=re.IGNORECASE)
        return self._sanitize_name(name) or "home"

    @staticmethod
    def _clean_image_url(raw_url: str) -> str:
        """
        Rimuove query string e normalizza URL di immagini.
        Gestisce anche pattern Wix con doppio percorso nell'URL.
        """
        url = raw_url.split("?")[0]
        # Pattern Wix: path termina con estensione immagine poi c'è altro
        wix_match = re.search(
            r"(.*\.(?:jpg|jpeg|png|webp|gif|svg))/",
            url, re.IGNORECASE
        )
        if wix_match:
            url = wix_match.group(1)
        return url

    # -----------------------------------------------------------------------
    # MODULO COMPRESSIONE: Comprime file enormi (> 25MB) in automatico
    # -----------------------------------------------------------------------

    def _compress_document_if_needed(self, file_path: Path, max_size_mb: float = 25.0) -> None:
        """
        Verifica la dimensione del file. Se supera max_size_mb, lo comprime
        in base alla sua estensione (.pdf, .docx, .png, ecc.) per scendere sotto il limite.
        """
        if not file_path.exists():
            return
        
        file_size_mb = file_path.stat().st_size / (1024 * 1024)
        if file_size_mb <= max_size_mb:
            return

        ext = file_path.suffix.lower()
        if ext == ".pdf":
            self._compress_pdf(file_path, max_size_mb)
        elif ext in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}:
            self._compress_office_doc(file_path, max_size_mb)
        elif ext in {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".bmp"}:
            self._compress_raw_image(file_path, max_size_mb)

    def _compress_pdf(self, file_path: Path, max_size_mb: float) -> None:
        try:
            import pymupdf
            
            # Parametri iterativi decrescenti (Risoluzione DPI, Qualità JPEG)
            # Partiamo da una qualità altissima per preservare i documenti il più possibile
            steps = [
                (300, 95),  # Iterazione 1: Qualità eccellente
                (250, 90),  # Iterazione 2: Qualità ottima
                (200, 85),  # Iterazione 3: Qualità molto buona
                (150, 80),  # Iterazione 4: Qualità buona
                (150, 70),  # Iterazione 5: Qualità media
                (120, 60),  # Iterazione 6: Qualità sufficiente
                (95, 50),   # Iterazione 7: Qualità minima (aggressiva)
            ]
            
            original_size_mb = file_path.stat().st_size / (1024 * 1024)
            current_path = file_path
            
            for idx, (dpi, qual) in enumerate(steps):
                doc = pymupdf.open(str(current_path))
                doc.rewrite_images(
                    dpi_threshold=dpi + 1,
                    dpi_target=dpi,
                    quality=qual,
                    lossy=True
                )
                if hasattr(doc, "subset_fonts"):
                    try:
                        doc.subset_fonts()
                    except Exception:
                        pass
                
                temp_path = file_path.with_suffix(f".tmp_pdf_{idx}.pdf")
                doc.save(str(temp_path), garbage=4, deflate=True)
                doc.close()
                
                new_size_mb = temp_path.stat().st_size / (1024 * 1024)
                print(f"   ⚡ PDF compress iterazione {idx+1} (DPI {dpi}, Qualità {qual}): {new_size_mb:.2f} MB")
                
                # Se abbiamo ottenuto un file sotto la soglia di 25 MB, ci fermiamo!
                if new_size_mb <= max_size_mb:
                    if current_path != file_path and current_path.exists():
                        current_path.unlink()
                    if file_path.exists():
                        file_path.unlink()
                    temp_path.rename(file_path)
                    print(f"   ✅ PDF compresso con successo a {new_size_mb:.2f} MB (soglia raggiunta con DPI {dpi}, Qualità {qual})")
                    return
                
                # Altrimenti, se è migliorato ma non sotto la soglia, usiamo questo file come base per l'iterazione successiva
                if current_path != file_path and current_path.exists():
                    current_path.unlink()
                current_path = temp_path
            
            # Se dopo tutti gli step non siamo scesi sotto la soglia, usiamo comunque la versione più leggera
            if current_path != file_path and current_path.exists():
                if file_path.exists():
                    file_path.unlink()
                current_path.rename(file_path)
                final_size = file_path.stat().st_size / (1024 * 1024)
                print(f"   ✅ PDF ottimizzato al massimo possibile: {final_size:.2f} MB")
                
        except Exception as exc:
            msg = f"Errore durante la compressione del PDF: {exc}"
            print(f"   ⚠️  {msg}")
            logger.error(msg, exc_info=True)


    def _compress_office_doc(self, file_path: Path, max_size_mb: float) -> None:
        import zipfile
        import tempfile
        import shutil
        import os
        from PIL import Image

        original_size_mb = file_path.stat().st_size / (1024 * 1024)
        
        # Iteriamo riducendo la qualità ed il ridimensionamento delle immagini in passaggi gradualmente più stretti
        steps = [
            (2560, 90), # Iterazione 1: Risoluzione 2.5K, Qualità JPEG 90
            (1920, 80), # Iterazione 2: Risoluzione 1080p, Qualità JPEG 80
            (1600, 70), # Iterazione 3: Risoluzione 1600px, Qualità JPEG 70
            (1280, 60), # Iterazione 4: Risoluzione 720p, Qualità JPEG 60
            (1024, 45)  # Iterazione 5: Risoluzione 1024px, Qualità JPEG 45 (limite)
        ]

        for idx, (max_dim, qual) in enumerate(steps):
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    with zipfile.ZipFile(file_path, 'r') as zip_ref:
                        zip_ref.extractall(tmpdir)

                    media_dirs = [
                        Path(tmpdir) / "word" / "media",
                        Path(tmpdir) / "xl" / "media",
                        Path(tmpdir) / "ppt" / "media",
                        Path(tmpdir) / "Pictures"
                    ]

                    image_extensions = {".jpg", ".jpeg", ".png"}
                    for mdir in media_dirs:
                        if mdir.exists() and mdir.is_dir():
                            for img_file in mdir.glob("*"):
                                if img_file.suffix.lower() in image_extensions:
                                    try:
                                        with Image.open(img_file) as img:
                                            w, h = img.size
                                            if w > max_dim or h > max_dim:
                                                img.thumbnail((max_dim, max_dim))
                                            
                                            if img_file.suffix.lower() in {".jpg", ".jpeg"}:
                                                img.save(img_file, "JPEG", quality=qual, optimize=True)
                                            elif img_file.suffix.lower() == ".png":
                                                compress_lvl = 6 if qual >= 90 else (7 if qual >= 80 else 9)
                                                img.save(img_file, "PNG", compress_level=compress_lvl)
                                    except Exception:
                                        pass

                    temp_zip = file_path.with_suffix(f".tmp_office_{idx}.zip")
                    with zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED) as zip_out:
                        for root, dirs, files in os.walk(tmpdir):
                            for file in files:
                                full_path = Path(root) / file
                                rel_path = full_path.relative_to(tmpdir)
                                zip_out.write(full_path, rel_path)

                    new_size_mb = temp_zip.stat().st_size / (1024 * 1024)
                    print(f"   ⚡ Office compress iterazione {idx+1} (Risoluzione {max_dim}, Qualità {qual}): {new_size_mb:.2f} MB")

                    if new_size_mb <= max_size_mb:
                        file_path.unlink()
                        temp_zip.rename(file_path)
                        print(f"   ✅ Documento Office compresso con successo a {new_size_mb:.2f} MB (soglia raggiunta)")
                        return
                    else:
                        # Se è migliorato ma non sotto la soglia, sovrascriviamo l'originale come base per la successiva
                        if new_size_mb < (file_path.stat().st_size / (1024 * 1024)):
                            file_path.unlink()
                            temp_zip.rename(file_path)
                        else:
                            if temp_zip.exists():
                                temp_zip.unlink()
            except Exception as exc:
                msg = f"Errore iterazione compressione Office: {exc}"
                print(f"   ⚠️  {msg}")
                logger.error(msg, exc_info=True)


        print(f"   ✅ Documento Office ottimizzato al massimo possibile: {file_path.stat().st_size / (1024 * 1024):.2f} MB")

    def _compress_raw_image(self, file_path: Path, max_size_mb: float) -> None:
        from PIL import Image
        
        original_size_mb = file_path.stat().st_size / (1024 * 1024)
        
        steps = [
            (3840, 90), # Iterazione 1: Risoluzione 4K, Qualità 90
            (2560, 80), # Iterazione 2: Risoluzione 2.5K, Qualità 80
            (1920, 70), # Iterazione 3: Risoluzione 1080p, Qualità 70
            (1280, 55), # Iterazione 4: Risoluzione 720p, Qualità 55
        ]
        
        for idx, (max_dim, qual) in enumerate(steps):
            try:
                with Image.open(file_path) as img:
                    w, h = img.size
                    if w > max_dim or h > max_dim:
                        img.thumbnail((max_dim, max_dim))
                    
                    ext = file_path.suffix.lower()
                    temp_img = file_path.with_suffix(f".tmp_img_{idx}" + ext)
                    
                    if ext in {".jpg", ".jpeg"}:
                        img.save(temp_img, "JPEG", quality=qual, optimize=True, progressive=True)
                    elif ext == ".png":
                        compress_lvl = 6 if qual >= 90 else (7 if qual >= 80 else 9)
                        img.save(temp_img, "PNG", compress_level=compress_lvl)
                    elif ext == ".webp":
                        img.save(temp_img, "WEBP", quality=qual, method=6)
                    else:
                        if img.mode in ("RGBA", "LA"):
                            img.save(temp_img, "PNG", compress_level=9)
                        else:
                            img.save(temp_img, "JPEG", quality=qual, optimize=True)

                    new_size_mb = temp_img.stat().st_size / (1024 * 1024)
                    print(f"   ⚡ Immagine compress iterazione {idx+1} (Risoluzione {max_dim}, Qualità {qual}): {new_size_mb:.2f} MB")
                    
                    if new_size_mb <= max_size_mb:
                        file_path.unlink()
                        temp_img.rename(file_path)
                        print(f"   ✅ Immagine compressa con successo a {new_size_mb:.2f} MB (soglia raggiunta)")
                        return
                    else:
                        if new_size_mb < (file_path.stat().st_size / (1024 * 1024)):
                            file_path.unlink()
                            temp_img.rename(file_path)
                        else:
                            if temp_img.exists():
                                temp_img.unlink()
            except Exception as exc:
                msg = f"Errore iterazione compressione immagine: {exc}"
                print(f"   ⚠️  {msg}")
                logger.error(msg, exc_info=True)


        print(f"   ✅ Immagine ottimizzata al massimo possibile: {file_path.stat().st_size / (1024 * 1024):.2f} MB")

    # -----------------------------------------------------------------------
    # UTILITY: Download generico
    # -----------------------------------------------------------------------

    def _download_file(
        self,
        url: str,
        dest_folder: Path,
        filename: str,
        referer: str = "",
        fallback_urls: list = None,
    ) -> bool:
        """
        Scarica un file remoto nella cartella di destinazione.
        Restituisce True se il download è avvenuto (o il file esiste già).

        - 'referer': header Referer obbligatorio per CDN come Wix, Cloudflare, ecc.
        - 'fallback_urls': lista di URL alternativi da provare se il primario
          risponde 404. Utile per Wix dove l'URL normalizzato (senza /v1/fill/)
          a volte non esiste e bisogna usare quello originale con parametri CDN.
        """
        if not filename:
            return False
        filename = filename.split("?")[0]
        dest_path = dest_folder / filename

        # Riferimento globale per i file già scaricati in questa sessione
        normalized = self._normalize_wix_url(url)
        with self._lock:
            if normalized in self.downloaded_files:
                existing_path = self.downloaded_files[normalized]
                if existing_path.exists() and not dest_path.exists():
                    import shutil
                    try:
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(existing_path, dest_path)
                        return True
                    except Exception:
                        pass

        if dest_path.exists():
            with self._lock:
                self.downloaded_files[normalized] = dest_path
            return True

        from urllib.parse import quote as _quote
        import time as _time

        def _build_headers(ref: str = "", target_url: str = "") -> dict:
            h = {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
            }
            if ref:
                h["Referer"] = _quote(ref, safe="/:@?=&#%+.,;!~*'()")
            return h

        def _save(response) -> bool:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                for chunk in response.iter_content(8192):
                    f.write(chunk)
            
            # Comprimi il file se supera i 25 MB in automatico
            self._compress_document_if_needed(dest_path)

            with self._lock:
                self.downloaded_files[normalized] = dest_path
            return True

        def _try_url(try_url: str):  # -> bool | None (Python 3.10+, qui rimosso per compatibilità 3.9)
            """
            Tenta il download da try_url con rotazione Referer.
            Ritorna True se OK, False se 404 definitivo, None se fallito (tenta next).
            """
            is_wix_cdn = "wixstatic.com" in try_url or "wixmp.com" in try_url

            attempts = [
                referer,
                f"{urlparse(try_url).scheme}://{urlparse(try_url).netloc}/",
                "",
            ]
            for i, ref in enumerate(attempts):
                try:
                    resp = requests.get(
                        try_url,
                        headers=_build_headers(ref, try_url),
                        timeout=20,
                        stream=True,
                        allow_redirects=True,
                    )
                    if resp.status_code in (200, 206):
                        return _save(resp)
                    if resp.status_code == 404:
                        # Wix CDN a volte restituisce 404 sotto carico concorrente
                        # (rate limiting mascherato). Ritenta una volta dopo breve pausa.
                        if is_wix_cdn and i == 0:
                            _time.sleep(1.0)
                            retry_resp = requests.get(
                                try_url,
                                headers=_build_headers(ref, try_url),
                                timeout=20,
                                stream=True,
                                allow_redirects=True,
                            )
                            if retry_resp.status_code in (200, 206):
                                return _save(retry_resp)
                        return None  # 404 → prova URL successivo nella catena
                    if resp.status_code == 429:
                        _time.sleep(2 * (i + 1))
                        continue
                    # 403, 401, 5xx → prossimo referer
                except requests.exceptions.Timeout as exc:
                    if i == len(attempts) - 1:
                        msg = f"Timeout di connessione definitivo [{filename[:60]}]: {exc}"
                        print(f"   ⚠️  {msg}")
                        logger.warning(msg, exc_info=True)
                        return None

                except Exception as exc:
                    msg = f"Download fallito [{filename[:60]}]: {exc}"
                    print(f"   ⚠️  {msg}")
                    logger.warning(msg, exc_info=True)
                    return None

            return None

        # Catena di URL da provare: primario + fallback
        url_chain = [url] + (fallback_urls or [])
        for try_url in url_chain:
            if not try_url:
                continue
            result = _try_url(try_url)
            if result is True:
                return True  # Scaricato con successo
            # result is None → 404 o errore, proviamo il prossimo URL

        # Tutti gli URL della catena hanno fallito
        msg = f"404 Not Found [{filename[:60]}]"
        print(f"   ⚠️  {msg}")
        logger.warning(msg)

        return False



    # -----------------------------------------------------------------------
    # MODULO FONT: _scrape_fonts()
    # -----------------------------------------------------------------------

    def _scrape_fonts(self, page: "Page", page_url: str, intercepted_font_urls: set) -> None:
        """
        Estrae tutti gli URL @font-face dalla pagina tramite l'API CSS del browser
        e dall'intercettazione dei pacchetti di rete, scarica i file font
        e li converte in TTF/OTF organizzandoli per famiglia.
        """
        try:
            # Raccoglie gli URL font dalle regole @font-face dei fogli di stile
            font_urls: list = page.evaluate("""() => {
                const urls = [];
                for (const sheet of document.styleSheets) {
                    try {
                        for (const rule of sheet.cssRules) {
                            if (rule.type === CSSRule.FONT_FACE_RULE) {
                                const src = rule.style.getPropertyValue('src');
                                const matches = src.match(/url\\(([^)]+)\\)/g);
                                if (matches) {
                                    matches.forEach(m => {
                                        let u = m.replace(/url\\([\"']?/, '')
                                                 .replace(/[\"']?\\)/, '').trim();
                                        urls.push(u);
                                    });
                                }
                            }
                        }
                    } catch (e) {}
                }
                // Raccoglie anche i <link preload as="font">
                document.querySelectorAll(
                    'link[as="font"], link[href*=".woff"], link[href*=".ttf"]'
                ).forEach(l => { if (l.href) urls.push(l.href); });
                return urls;
            }""")
        except Exception as exc:
            msg = f"Errore estrazione font JS: {exc}"
            print(f"   ⚠️  {msg}")
            logger.warning(msg, exc_info=True)
            font_urls = []


        all_font_urls = set(font_urls) | intercepted_font_urls
        for font_href in all_font_urls:
            # Normalizza e pulisce l'URL
            full_url = urljoin(page_url, font_href).split("?")[0].split("#")[0]
            font_name = unquote(Path(urlparse(full_url).path).name)

            # Applica la blacklist degli alfabeti inutili
            if any(kw in font_name.lower() for kw in FONT_BLACKLIST):
                continue

            ext = Path(font_name).suffix.lower()
            if ext not in FONT_EXTENSIONS:
                continue

            # Scarica nella cartella Fonts/ (radice) come file temporaneo
            if self._download_file(full_url, self.fonts_dir, font_name):
                font_path = self.fonts_dir / font_name
                if font_path.exists():
                    self._convert_and_organize_font(font_path)

    @staticmethod
    def _normalize_font_family(family: str) -> str:
        """
        Normalizza il nome della famiglia del font per raggruppare i vari pesi
        e varianti (es. Light, W01, Roman, Bold, SemiBold, LT) sotto una singola
        cartella radice pulita (es. 'DIN_Next', 'Futura', 'Helvetica', 'Cormorant_Garamond').
        """
        if not family:
            return "Unknown"
        
        # Pulisce spazi, trattini e underscore per lavorare su blocchi omogenei
        clean = family.replace("_", " ").replace("-", " ")
        
        # Parole chiave da rimuovere che indicano pesi, stili, formati o distributori
        remove_keywords = {
            # Pesi / Stili
            "light", "semibold", "bold", "italic", "regular", "medium", "thin", 
            "extrabold", "extralight", "ultralight", "heavy", "black", "condensed", 
            "book", "roman", "oblique", "demi", "extra", "ultra", "narrow",
            # Distributori / Tag di formato / Versioni
            "lt", "mt", "vf", "pro", "std", "w01", "w02", "w04", "w05", "w10", "w25", "w06", "w07", "w08", "w09"
        }
        
        # Rimuove le parole chiave mantenendo l'ordine
        words = clean.split()
        normalized_words = []
        for word in words:
            if word.lower() not in remove_keywords:
                normalized_words.append(word)
                
        if normalized_words:
            result = "_".join(normalized_words)
        else:
            result = family

        # Pulisce trattini o underscore multipli
        import re
        result = re.sub(r"_+", "_", result).strip("_")
        
        # Mappatura manuale di override per famiglie ben note per garantire raggruppamento ideale
        lower_result = result.lower()
        if "cormorant" in lower_result:
            return "Cormorant_Garamond"
        if "din_next" in lower_result or "dinnext" in lower_result:
            return "DIN_Next"
        if "futura" in lower_result:
            return "Futura"
        if "helvetica" in lower_result:
            return "Helvetica"
        if "avenir" in lower_result:
            return "Avenir"
        if "proxima" in lower_result:
            return "Proxima_Nova"
        if "wix_madefor" in lower_result:
            return "Wix_Madefor"
        if "cinzel" in lower_result:
            return "Cinzel"
            
        return result

    def _convert_and_organize_font(self, font_path: Path) -> None:
        """
        Converte woff/woff2 in TTF, estrae metadati (nameID 1 e 4),
        organizza in sottocartelle per famiglia ed elimina il file temporaneo.
        """
        try:
            from fontTools.ttLib import TTFont as _TTFont
        except ImportError:
            msg = "fontTools non disponibile — font non convertito."
            print(f"   ⚠️  {msg}")
            logger.warning(msg)
            return

        # Validazione: file troppo piccoli sono probabilmente download corrotti
        if font_path.exists() and font_path.stat().st_size < 100:
            msg = f"Font troppo piccolo, probabile download corrotto [{font_path.name}] ({font_path.stat().st_size} bytes)"
            logger.warning(msg)
            font_path.unlink(missing_ok=True)
            return

        try:
            font_obj = _TTFont(str(font_path))
        except Exception as exc:
            # Se è un errore brotli su WOFF2, il file potrebbe essere corrotto.
            # Tenta un ri-download diretto via requests con header corretti.
            if "brotli" in str(exc).lower() and font_path.suffix.lower() == ".woff2":
                msg = f"Brotli decompression fallita per [{font_path.name}], tentativo ri-download..."
                logger.warning(msg)
                try:
                    import requests as _req
                    # Cerca l'URL originale nella cache dei download
                    original_url = None
                    with self._lock:
                        for cached_url, cached_path in self.downloaded_files.items():
                            if cached_path == font_path or cached_path.name == font_path.name:
                                original_url = cached_url
                                break
                    if original_url:
                        resp = _req.get(original_url, timeout=15, headers={
                            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                                          "Chrome/122.0.0.0 Safari/537.36",
                            "Accept": "application/font-woff2,application/font-woff,*/*",
                        })
                        if resp.status_code == 200 and len(resp.content) > 100:
                            with open(font_path, "wb") as f:
                                f.write(resp.content)
                            font_obj = _TTFont(str(font_path))
                        else:
                            raise exc  # ri-solleva l'errore originale
                    else:
                        raise exc
                except Exception:
                    msg = f"Impossibile aprire il font [{font_path.name}]: {exc}"
                    print(f"   ⚠️  {msg}")
                    logger.warning(msg, exc_info=True)
                    return
            else:
                msg = f"Impossibile aprire il font [{font_path.name}]: {exc}"
                print(f"   ⚠️  {msg}")
                logger.warning(msg, exc_info=True)
                return


        try:
            real_name   = None
            font_family = None

            # Estrazione metadati interni (nameID 4 = nome completo, nameID 1 = famiglia)
            if "name" in font_obj:
                for record in font_obj["name"].names:
                    try:
                        text = record.toUnicode().strip()
                    except Exception:
                        continue
                    if record.nameID == 4 and not real_name:
                        real_name = text
                    if record.nameID == 1 and not font_family:
                        font_family = text
                    if real_name and font_family:
                        break

            # Fallback: usa il nome del file se i metadati sono assenti/corrotti
            if not real_name:
                real_name = font_path.stem
            if not font_family:
                font_family = real_name

            # Sanitizzazione dei nomi estratti con normalizzazione della famiglia
            real_name   = self._sanitize_name(real_name)
            font_family = self._normalize_font_family(font_family)
            font_family = self._sanitize_name(font_family)

            # Anti-duplicato globale (basato sul nome reale del font)
            if real_name in self.downloaded_font_keys:
                font_obj.close()
                if font_path.exists():
                    font_path.unlink()
                self.stats["fonts_duplicated"] += 1
                print(f"   ♻️  Font duplicato ignorato: {real_name}")
                return

            # Cartella famiglia: Fonts/[FamigliaFont]/
            # Rileva se contiene tracciati CFF (caratteristici di OpenType .otf)
            is_cff = "CFF " in font_obj or "CFF2" in font_obj
            ext = ".otf" if is_cff else ".ttf"

            # Cartella famiglia: Fonts/[FamigliaFont]/
            family_dir = self.fonts_dir / font_family
            family_dir.mkdir(parents=True, exist_ok=True)

            dest_path = family_dir / f"{real_name}{ext}"

            # Anti-duplicato fisico
            if dest_path.exists():
                font_obj.close()
                if font_path.exists():
                    font_path.unlink()
                self.downloaded_font_keys.add(real_name)
                print(f"   ♻️  Font già presente: {real_name}{ext}")
                return

            if font_path.suffix.lower() in (".woff", ".woff2"):
                # Converte woff/woff2 → OTF/TTF rimuovendo il flavor
                font_obj.flavor = None
                font_obj.save(str(dest_path))
                font_obj.close()
                if font_path.exists():
                    font_path.unlink()  # Rimuove il file temporaneo originale
                self.stats["fonts_converted"] += 1
                print(f"   🔤 Font convertito → [{font_family}]: {real_name}{ext}")
            else:
                # TTF/OTF già decompresso: sposta nella cartella famiglia
                font_obj.close()
                if font_path != dest_path:
                    if font_path.exists():
                        font_path.rename(dest_path)
                print(f"   💎 Font organizzato → [{font_family}]: {real_name}{ext}")

            self.downloaded_font_keys.add(real_name)

        except Exception as exc:
            msg = f"Errore organizzazione font [{font_path.name}]: {exc}"
            print(f"   ⚠️  {msg}")
            logger.warning(msg, exc_info=True)

            try:
                font_obj.close()
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # MODULO DOCUMENTI: _scrape_documents()
    # -----------------------------------------------------------------------

    def _scrape_documents(self, page: "Page", page_url: str, page_name: str) -> None:
        """
        Scansiona tutti gli <a href> della pagina e scarica i documenti
        con estensioni supportate (.pdf, .doc, .docx, .xls, .ppt, …).
        Salva i documenti in sottocartelle specifiche per ciascuna pagina.
        """
        try:
            link_elements = page.query_selector_all("a[href]")
        except Exception:
            return

        for link_el in link_elements:
            try:
                href = link_el.get_attribute("href")
                if not href:
                    continue
                full_url = urljoin(page_url, href).split("?")[0]
                ext = Path(urlparse(full_url).path).suffix.lower()

                if ext in DOCUMENT_EXTENSIONS:
                    filename = Path(urlparse(full_url).path).name
                    if filename:
                        page_docs_dir = self.documents_dir / page_name
                        if self._download_file(full_url, page_docs_dir, filename):
                            self.stats["documents_downloaded"] += 1
                            print(f"   📄 Documento scaricato: {filename}")
            except Exception as exc:
                msg = f"Errore link documento: {exc}"
                print(f"   ⚠️  {msg}")
                logger.warning(msg, exc_info=True)


    # -----------------------------------------------------------------------
    # MODULO TESTI: _save_text()
    # -----------------------------------------------------------------------

    def _save_text(self, soup: BeautifulSoup, page_name: str) -> None:
        """
        Estrae il contenuto testuale pulito dalla pagina tramite BeautifulSoup
        e lo salva in Text/[NomePagina].txt.
        """
        try:
            # Rimuove script, stili e elementi non testuali
            for tag in soup(["script", "style", "noscript", "head", "meta", "link"]):
                tag.decompose()

            # Estrazione con separatore newline (come da specifiche)
            text_content = soup.get_text(separator="\n")

            # Pulizia: rimuove righe vuote consecutive
            lines = [line.strip() for line in text_content.splitlines()]
            cleaned_lines = []
            prev_blank = False
            for line in lines:
                if line:
                    cleaned_lines.append(line)
                    prev_blank = False
                elif not prev_blank:
                    cleaned_lines.append("")
                    prev_blank = True

            dest_path = self.text_dir / f"{page_name}.txt"
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "w", encoding="utf-8") as f:
                f.write("\n".join(cleaned_lines))

            self.stats["texts_saved"] += 1
            print(f"   📝 Testo salvato: {page_name}.txt")
        except Exception as exc:
            msg = f"Errore salvataggio testo [{page_name}]: {exc}"
            print(f"   ⚠️  {msg}")
            logger.warning(msg, exc_info=True)


    # -----------------------------------------------------------------------
    # MODULO WIX STORES: estrazione prodotti e product-page URL
    # -----------------------------------------------------------------------

    # App ID Wix Stores (costante pubblica Wix, non cambia tra siti)
    _WIX_STORES_APP_ID = "1380b703-ce81-ff05-f115-39571d94dfcd"

    def _extract_wix_store_products(self, html_content: str, page_url: str) -> None:
        """
        Estrae i prodotti da Wix Stores leggendo il blocco JSON wix-warmup-data.

        Wix Stores memorizza i dati in due strutture distinte:

        A) PAGINA CATEGORIA  (es. /category/all-products)
           appsWarmupData[WIX_STORES_APP_ID]
             └── "products_*"  →  { list: [...], totalCount: N }

           Ogni item della lista contiene i campi minimi:
             name, price, formattedPrice, sku, urlPart, productType, media[], isInStock
           (la descrizione completa è assente — serve navigare la pagina prodotto)

        B) PAGINA PRODOTTO SINGOLO  (es. /product-page/SLUG)
           appsWarmupData[WIX_STORES_APP_ID]
             └── "productPage_*"  →  { catalog: { product: {...} }, appSettings: {...} }

           Il prodotto ha TUTTI i campi:
             name, price, description (HTML), sku, brand, weight,
             media[], options[], categories[], additionalInfo[]
        """
        warmup_script = re.search(
            r'<script[^>]+id="wix-warmup-data"[^>]*>(.*?)</script>',
            html_content, re.DOTALL | re.IGNORECASE
        )
        if not warmup_script:
            return

        try:
            data = json.loads(warmup_script.group(1))
        except Exception:
            return

        apps = data.get("appsWarmupData", {})
        store_app = apps.get(self._WIX_STORES_APP_ID, {})
        if not store_app:
            return

        # Calcola base URL del sito per costruire le URL dei prodotti
        parsed_base = urlparse(page_url)
        site_base   = f"{parsed_base.scheme}://{parsed_base.netloc}"
        # Wix wixsite.com usa /SITENAME/ come prefisso
        path_parts  = parsed_base.path.split("/")
        site_prefix = "/".join(path_parts[:2]) if len(path_parts) > 2 else ""

        def _html_to_text(html_desc: str) -> str:
            """Converte description HTML Wix in testo pulito per il CSV."""
            if not html_desc:
                return ""
            try:
                from bs4 import BeautifulSoup as _BS
                text = _BS(html_desc, "html.parser").get_text(separator=" ")
                return re.sub(r"\s+", " ", text).strip()
            except Exception:
                return re.sub(r"<[^>]+>", " ", html_desc).strip()

        def _build_product_row(prod: dict, page_url: str) -> dict:
            """Trasforma un dict prodotto Wix nel formato CSV Flazio."""
            name   = prod.get("name", "").strip()
            if not name:
                return {}

            # Prezzo: preferisce il formattedPrice (già con valuta), poi float
            price_raw = prod.get("formattedPrice", "")
            if not price_raw:
                price_raw = str(prod.get("price", ""))
            # Normalizza: rimuove simboli valuta, mantiene solo cifre e separatori
            price_clean = re.sub(r"[^\d.,]", "", price_raw).replace(",", ".")
            # Rimuovi doppi punti (es. "14.99." → "14.99")
            price_clean = re.sub(r"\.{2,}", ".", price_clean).strip(".")

            # SKU
            sku = str(prod.get("sku", "")).strip()

            # Tipo prodotto → STATUS
            prod_type  = prod.get("productType", "physical")  # physical / digital
            is_in_stock = prod.get("isInStock", True)
            inventory   = prod.get("inventory", {})
            inv_status  = inventory.get("status", "in_stock") if isinstance(inventory, dict) else "in_stock"
            status = "instock" if (is_in_stock or inv_status == "in_stock") else "outofstock"

            # Descrizione (disponibile solo nella pagina prodotto singolo)
            desc_html = prod.get("description", "")
            desc      = _html_to_text(desc_html)

            # Brand
            brand_val = prod.get("brand")
            brand     = (brand_val.get("name", "") if isinstance(brand_val, dict) else str(brand_val or "")).strip()

            # Weight
            weight = str(prod.get("weight") or "").strip()
            if weight == "0" or weight == "0.0":
                weight = ""

            # Categoria principale
            categories = prod.get("categories", []) or []
            cats_str = ",".join(
                c.get("name", "") for c in categories
                if isinstance(c, dict) and c.get("name") and c.get("name") != "All Products"
            )

            # Immagine principale (fullUrl della prima media)
            media_list = prod.get("media", []) or []
            img_url = ""
            if media_list:
                first_media = media_list[0]
                if isinstance(first_media, dict):
                    raw_img = first_media.get("fullUrl") or first_media.get("url", "")
                    # Normalizza: rimuovi parametri /v1/fit/ per ottenere l'originale
                    img_url = self._normalize_wix_url(raw_img.split("?")[0]) if raw_img else ""

            # Opzioni (varianti prodotto)
            options_list = prod.get("options", []) or []
            options_parts = []
            for opt in options_list:
                if isinstance(opt, dict):
                    title  = opt.get("title", "")
                    values = [c.get("value", "") for c in opt.get("choices", [])
                              if isinstance(c, dict)]
                    if title and values:
                        options_parts.append(f"{title}:{','.join(values)}")
            options_str = ";".join(options_parts)

            # ID stabile basato sul product ID Wix (più affidabile dell'URL hash)
            prod_id_raw = prod.get("id", "") or prod.get("urlPart", "") or page_url
            prod_id = f"PROD_{abs(hash(prod_id_raw)) % 100000:05d}"

            return {
                "MODEL":       "product",
                "ID":          prod_id,
                "REF":         sku or prod_id,
                "CODE":        sku or prod_id,
                "TYPE":        "product",
                "NAME":        name,
                "DESCRIPTION": desc,
                "STATUS":      status,
                "VAT":         "22",
                "OPTIONS":     options_str,
                "PRICE":       price_clean,
                "QUANTITY":    "10" if status == "instock" else "0",
                "WEIGHT":      weight,
                "CATEGORIES":  cats_str,
                "TAGS":        "",
                "IMAGE":       img_url,
                "BRAND":       brand,
            }

        def _add_product(row: dict) -> None:
            """Aggiunge il prodotto alla lista se non è un duplicato."""
            if not row or not row.get("NAME"):
                return
            with self._lock:
                if any(p["NAME"] == row["NAME"] for p in self.detected_products):
                    return
                self.detected_products.append(row)
            print(f"   🛒 Wix Store prodotto: {row['NAME']}"
                  f"{'  →  €' + row['PRICE'] if row['PRICE'] else ''}")

        # ── A) Pagina categoria: chiave products_* ────────────────────────
        for key, val in store_app.items():
            if key.startswith("products_") and isinstance(val, dict):
                prod_list = val.get("list", [])
                total     = val.get("totalCount", 0)
                if total > len(prod_list):
                    msg = (f"Wix Store: {total} prodotti totali, "
                           f"solo {len(prod_list)} nel payload. "
                           "Naviga /category/all-products per la lista completa.")
                    print(f"   ⚠️  {msg}")
                    logger.warning(msg)

                for prod in prod_list:
                    if isinstance(prod, dict):
                        row = _build_product_row(prod, page_url)
                        _add_product(row)

        # ── B) Pagina prodotto singolo: chiave productPage_* ──────────────
        for key, val in store_app.items():
            if key.startswith("productPage_") and isinstance(val, dict):
                catalog = val.get("catalog", {})
                prod    = catalog.get("product") if isinstance(catalog, dict) else None
                if prod and isinstance(prod, dict):
                    row = _build_product_row(prod, page_url)
                    _add_product(row)

    def _inject_wix_store_product_urls(self, html_content: str, page_url: str) -> None:
        """
        Aggiunge alla coda di crawling le URL delle singole pagine prodotto
        Wix trovate nella pagina categoria corrente.

        Legge i prodotti dal warmup-data e costruisce le URL nel formato:
          BASE_URL/product-page/SLUG
        dove SLUG = product["urlPart"].

        Questo garantisce che ogni prodotto venga visitato singolarmente,
        così si ottengono i dati completi (descrizione, opzioni, ecc.).
        """
        warmup_script = re.search(
            r'<script[^>]+id="wix-warmup-data"[^>]*>(.*?)</script>',
            html_content, re.DOTALL | re.IGNORECASE
        )
        if not warmup_script:
            return

        try:
            data = json.loads(warmup_script.group(1))
        except Exception:
            return

        apps      = data.get("appsWarmupData", {})
        store_app = apps.get(self._WIX_STORES_APP_ID, {})
        if not store_app:
            return

        # Calcola il base URL per le product-page
        # Strategia: cerca il productPageBaseUrl nei link <a> della pagina
        prod_links = re.findall(
            r'href="([^"]*product-page/[^"]+)"', html_content, re.IGNORECASE
        )
        if prod_links:
            # Usa il base del primo link trovato
            sample  = prod_links[0]
            base_url = sample.rsplit("/product-page/", 1)[0]
        else:
            # Fallback: costruisce il base dall'externalBaseUrl nel viewer model
            ext_base = re.search(r'"externalBaseUrl"\s*:\s*"([^"]+)"', html_content)
            base_url = ext_base.group(1).replace("\\/", "/") if ext_base else ""
            if not base_url:
                base_url = page_url.rsplit("/", 1)[0]

        added = 0
        for key, val in store_app.items():
            if key.startswith("products_") and isinstance(val, dict):
                for prod in val.get("list", []):
                    url_part = prod.get("urlPart", "") if isinstance(prod, dict) else ""
                    if not url_part:
                        continue
                    product_url = f"{base_url}/product-page/{url_part}"
                    with self._lock:
                        if (
                            product_url not in self.visited_urls
                            and product_url not in self.pages_to_crawl
                        ):
                            self.pages_to_crawl.add(product_url)
                            added += 1

        if added:
            print(f"   🔗 Wix Store: {added} pagine prodotto aggiunte alla coda")

    # -----------------------------------------------------------------------
    # MODULO PRODOTTI: _process_product()
    # -----------------------------------------------------------------------

    def _process_product(self, soup: BeautifulSoup, page_url: str) -> None:
        """
        Analisi della pagina per rilevare prodotti.
        Ordine di priorità:
          0. Wix Stores (wix-warmup-data, gestito da _extract_wix_store_products)
          1. Dati strutturati JSON-LD (@type: Product)
          2. Meta tag Open Graph / Twitter Card
          3. Heuristica DOM (classe/id con 'product', prezzo visibile)
        Aggiunge il prodotto a self.detected_products se rilevato.

        NOTA: Per Wix Stores, i prodotti vengono estratti da _extract_wix_store_products
        chiamato in _process_page PRIMA di questo metodo. Qui gestiamo solo il fallback
        per altri CMS (WooCommerce, Shopify, Prestashop, ecc.).
        """
        p_name  = None
        p_price = ""
        p_desc  = ""
        p_image = ""
        p_cats  = ""
        p_brand = ""

        # --- Strategia 1: JSON-LD strutturato ---
        def _is_product_type(t) -> bool:
            """Verifica se @type è 'Product' (stringa o lista)."""
            if isinstance(t, str):
                return t == "Product"
            if isinstance(t, list):
                return "Product" in t
            return False

        for script_tag in soup.find_all("script", type="application/ld+json"):
            try:
                raw_text = script_tag.string or ""
                if not raw_text.strip():
                    continue
                data = json.loads(raw_text)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    # Supporta sia {"@type": "Product"} che {"@graph": [...]}
                    prod = None
                    if isinstance(item, dict):
                        if _is_product_type(item.get("@type")):
                            prod = item
                        elif "@graph" in item and isinstance(item["@graph"], list):
                            prod = next(
                                (x for x in item["@graph"]
                                 if isinstance(x, dict) and _is_product_type(x.get("@type"))),
                                None,
                            )

                        if prod:
                            p_name  = prod.get("name", "")
                            p_desc  = prod.get("description", "")
                            # Brand: può essere stringa o {"@type":"Brand","name":"..."}
                            brand_val = prod.get("brand")
                            if isinstance(brand_val, dict):
                                p_brand = brand_val.get("name", "")
                            elif isinstance(brand_val, str):
                                p_brand = brand_val

                            # Estrazione prezzo da offers (lista o singolo)
                            offers = prod.get("offers")
                            if isinstance(offers, list) and offers:
                                p_price = str(offers[0].get("price", ""))
                            elif isinstance(offers, dict):
                                p_price = str(offers.get("price", ""))

                            # Estrazione immagine (stringa, lista, o oggetto {url:...})
                            img = prod.get("image")
                            if isinstance(img, list) and img:
                                first = img[0]
                                p_image = first.get("url", first) if isinstance(first, dict) else first
                            elif isinstance(img, dict):
                                p_image = img.get("url", "")
                            elif isinstance(img, str):
                                p_image = img

                            break
            except Exception:
                pass
            if p_name:
                break

        # --- Completamento dati da meta tag OG (anche se JSON-LD ha trovato il nome) ---
        # Wix Stores mette il prezzo SOLO nei meta tag (product:price:amount),
        # NON nel JSON-LD offers. Quindi dobbiamo sempre controllare i meta.
        meta_price_tag = (
            soup.find("meta", attrs={"property": "product:price:amount"})
            or soup.find("meta", attrs={"property": "og:price:amount"})
        )
        if meta_price_tag and not p_price:
            p_price = meta_price_tag.get("content", "")

        meta_og_img = soup.find("meta", attrs={"property": "og:image"})
        if meta_og_img and not p_image:
            p_image = meta_og_img.get("content", "")

        meta_og_desc = (
            soup.find("meta", attrs={"property": "og:description"})
            or soup.find("meta", attrs={"name": "description"})
        )
        if meta_og_desc and not p_desc:
            p_desc = meta_og_desc.get("content", "")

        # Pulizia titolo: rimuove " | Nome Sito" aggiunto da Wix e altri CMS
        if p_name and " | " in p_name:
            p_name = p_name.split(" | ")[0].strip()

        # --- Strategia 2: Meta tag OG / Twitter (usata solo se JSON-LD assente) ---
        if not p_name:
            meta_title = (
                soup.find("meta", attrs={"property": "og:title"})
                or soup.find("meta", attrs={"name": "twitter:title"})
            )
            # Verifica che la pagina sembri davvero un prodotto (c'è un prezzo meta)
            meta_price_tag2 = (
                soup.find("meta", attrs={"property": "product:price:amount"})
                or soup.find("meta", attrs={"property": "og:price:amount"})
                or soup.find("meta", attrs={"property": "og:type",
                                           "content": re.compile(r"product", re.I)})
            )
            # Considera prodotto anche se og:type=product (anche senza price meta)
            og_type = soup.find("meta", attrs={"property": "og:type"})
            is_product_og = og_type and "product" in (og_type.get("content", "") or "").lower()

            if meta_title and (meta_price_tag2 or is_product_og):
                raw_title = meta_title.get("content", "")
                # Rimuove " | Nome Sito" se presente
                p_name = raw_title.split(" | ")[0].strip() if " | " in raw_title else raw_title
                if meta_price_tag2 and hasattr(meta_price_tag2, 'get') and meta_price_tag2.get('property') != 'og:type':
                    p_price = meta_price_tag2.get("content", "")

                meta_img = (
                    soup.find("meta", attrs={"property": "og:image"})
                    or soup.find("meta", attrs={"name": "twitter:image"})
                )
                if meta_img and not p_image:
                    p_image = meta_img.get("content", "")

                meta_desc = (
                    soup.find("meta", attrs={"property": "og:description"})
                    or soup.find("meta", attrs={"name": "description"})
                )
                if meta_desc and not p_desc:
                    p_desc = meta_desc.get("content", "")

        # --- Strategia 3: Heuristica DOM (ampia copertura multi-piattaforma) ---
        if not p_name:
            # Selettori CSS-class / ID comuni su WooCommerce, Shopify, Prestashop,
            # Magento, PrestaShop, siti custom e microdata schema.org inline.
            product_selectors = [
                # Schema.org microdata HTML (itemtype nel tag)
                {"itemtype": re.compile(r"schema\.org/Product", re.I)},
                # Classi/ID generici con "product" nel nome
                {"class": re.compile(r"product[_-]?(detail|title|name|info|page|single)", re.I)},
                {"id":    re.compile(r"product[_-]?(detail|title|name|info|page|single)", re.I)},
                # WooCommerce
                {"class": re.compile(r"woocommerce.*product|product.*woocommerce", re.I)},
                {"class": re.compile(r"summary\.entry-summary", re.I)},
                # Shopify
                {"class": re.compile(r"product__(title|info|form|description)", re.I)},
                {"class": re.compile(r"ProductForm|product-form", re.I)},
                # Prestashop
                {"class": re.compile(r"product-detail|product-description|pb-center-column", re.I)},
                # Magento
                {"class": re.compile(r"product-info-main|catalog-product-view", re.I)},
                # Generici e-commerce
                {"class": re.compile(r"item[_-]?(detail|title|name|description)", re.I)},
                {"class": re.compile(r"(single|detail)[_-]?product", re.I)},
            ]
            product_container = None
            for attrs in product_selectors:
                product_container = soup.find(True, attrs=attrs)
                if product_container:
                    break

            # Fallback finale: se c'è un <h1> E un prezzo visibile nella pagina
            if not product_container:
                price_match_full = re.search(
                    r"(?:€|\$|£|EUR|USD)\s*[\d]+[.,][\d]{2}",
                    soup.get_text()
                )
                if price_match_full:
                    # Usa il body come container di fallback
                    product_container = soup.find("body")

            if product_container:
                # Titolo: primo h1, poi h2
                heading = product_container.find("h1") or product_container.find("h2")
                if heading:
                    p_name = heading.get_text(strip=True)

                # Prezzo: cerca pattern €/$/£ seguito da cifre
                price_text = product_container.get_text()
                price_match = re.search(
                    r"(?:€|\$|£|EUR|USD)\s*([\d]+[.,][\d]{2})", price_text
                )
                if price_match:
                    p_price = price_match.group(1).replace(",", ".")

                # Immagine: primo img nel container con src valido
                img_tag = product_container.find(
                    "img", src=re.compile(r"https?://", re.I)
                ) or product_container.find("img")
                if img_tag:
                    p_image = (
                        img_tag.get("src", "")
                        or img_tag.get("data-src", "")
                        or img_tag.get("data-lazy-src", "")
                    )

        # --- Costruzione e validazione del record prodotto ---
        if not p_name:
            return  # Non è una pagina prodotto, skip

        # Normalizzazione prezzo
        if p_price:
            p_price = re.sub(r"[^\d.,]", "", str(p_price)).replace(",", ".")
        else:
            p_price = ""

        # Normalizzazione immagine
        if p_image:
            if isinstance(p_image, dict):
                p_image = p_image.get("url", "")
            elif isinstance(p_image, list) and p_image:
                first = p_image[0]
                p_image = first.get("url", first) if isinstance(first, dict) else first
            
            if isinstance(p_image, str) and p_image.strip():
                p_image = self._clean_image_url(urljoin(page_url, p_image.strip()))
            else:
                p_image = ""

        # Generazione ID stabile basato sull'URL (più affidabile dell'hash del nome)
        prod_id = f"PROD_{abs(hash(page_url)) % 100000:05d}"

        product_row = {
            "MODEL":       "product",
            "ID":          prod_id,
            "REF":         prod_id,
            "CODE":        prod_id,
            "TYPE":        "product",
            "NAME":        str(p_name).strip(),
            "DESCRIPTION": re.sub(r"\s+", " ", str(p_desc)).strip(),
            "STATUS":      "instock",
            "VAT":         "22",
            "OPTIONS":     "",
            "PRICE":       p_price,
            "QUANTITY":    "10",
            "WEIGHT":      "",
            "CATEGORIES":  p_cats,
            "TAGS":        "",
            "IMAGE":       p_image,
            "BRAND":       p_brand,
        }

        # Anti-duplicato: non aggiunge prodotti con lo stesso nome
        if not any(p["NAME"] == product_row["NAME"] for p in self.detected_products):
            self.detected_products.append(product_row)
            print(f"   🛍️  Prodotto rilevato: {product_row['NAME']}"
                  f"{'  →  €' + product_row['PRICE'] if product_row['PRICE'] else ''}")

    # -----------------------------------------------------------------------
    # MODULO CSV: _write_csv()
    # -----------------------------------------------------------------------

    def _write_csv(self) -> None:
        """
        Genera il file products_[Nome_Sito].csv nella root del sito,
        usando lo schema CSV obbligatorio Flazio con separatore ';'.
        Le celle senza dato restano vuote. Nessuna colonna extra.
        """
        with open(self.csv_path, mode="w", newline="", encoding="utf-8-sig") as f:
            # utf-8-sig aggiunge il BOM per compatibilità con Excel italiano
            writer = csv.DictWriter(
                f,
                fieldnames=FLAZIO_CSV_HEADERS,
                delimiter=";",
                extrasaction="ignore",  # Ignora eventuali chiavi extra nei dict
            )
            writer.writeheader()
            for prod in self.detected_products:
                # Assicura che tutte le colonne obbligatorie esistano (vuote se assenti)
                row = {col: prod.get(col, "") for col in FLAZIO_CSV_HEADERS}
                writer.writerow(row)

        print(f"\n   📊 CSV generato: {self.csv_path.name}")
        print(f"   📦 Prodotti schedati: {len(self.detected_products)}")

    # -----------------------------------------------------------------------
    # NAVIGAZIONE: link interni e sitemap
    # -----------------------------------------------------------------------

    def _extract_internal_links(self, soup: BeautifulSoup, current_url: str) -> None:
        """
        Raccoglie tutti i link interni della pagina e li aggiunge alla coda
        di navigazione, evitando URL già visitati o non pertinenti.
        """
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            full_url = urljoin(current_url, href)
            parsed   = urlparse(full_url)

            # Normalizzazione: rimuove fragment e query string
            clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            clean_url = clean_url.rstrip("/")

            # Verifica: stesso dominio, non già visitato, non un file binario
            url_domain = parsed.netloc.replace("www.", "")
            skip_exts  = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".zip",
                          ".doc", ".docx", ".xls", ".ppt")
            if (
                url_domain == self.domain
                and clean_url not in self.visited_urls
                and clean_url not in self.pages_to_crawl
                and not any(clean_url.lower().endswith(e) for e in skip_exts)
                and parsed.scheme in ("http", "https")
            ):
                self.pages_to_crawl.add(clean_url)

    def _load_sitemap(self) -> None:
        """
        Tenta di caricare la sitemap XML per popolare la coda di URL
        prima dell'inizio della scansione classica.
        Gestisce sia sitemap semplici che sitemap index (con puntatori ad altre sitemap).
        """
        # Estensioni NON-HTML da escludere anche dalla sitemap
        NON_HTML_EXTS = (".xml", ".txt", ".rss", ".atom", ".pdf",
                         ".jpg", ".jpeg", ".png", ".webp", ".gif",
                         ".zip", ".doc", ".docx", ".xls", ".ppt")

        def _add_url(url: str) -> bool:
            """Aggiunge un URL HTML alla coda, filtrando non-HTML e altri domini."""
            parsed = urlparse(url)
            clean  = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
            path_lower = parsed.path.lower()
            if (
                parsed.netloc.replace("www.", "") == self.domain
                and not any(path_lower.endswith(e) for e in NON_HTML_EXTS)
                and clean not in self.visited_urls
            ):
                self.pages_to_crawl.add(clean)
                return True
            return False

        def _fetch_sitemap(sitemap_url: str) -> list:
            """Scarica una sitemap e restituisce tutti gli <loc> trovati."""
            try:
                resp = requests.get(
                    sitemap_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    return re.findall(
                        r"<loc>\s*(https?://[^\s<]+)\s*</loc>",
                        resp.text, re.IGNORECASE
                    )
            except Exception:
                pass
            return []

        print("\ud83d\udd0d Ricerca sitemap XML in corso...")
        added = 0

        # Prova prima sitemap.xml standard
        urls = _fetch_sitemap(urljoin(self.target_url, "/sitemap.xml"))
        if not urls:
            # Fallback: prova /sitemap_index.xml (Wix, WordPress, ecc.)
            urls = _fetch_sitemap(urljoin(self.target_url, "/sitemap_index.xml"))

        # Riconosce sitemap index (contiene link ad altre sitemap XML)
        for url in urls:
            if url.lower().endswith(".xml"):
                # È una sitemap index: scarica ricorsivamente
                sub_urls = _fetch_sitemap(url)
                for sub_url in sub_urls:
                    if _add_url(sub_url):
                        added += 1
            else:
                if _add_url(url):
                    added += 1

        if added:
            print(f"   \u2705 Sitemap caricata \u2014 {added} URL HTML aggiunti.")
        else:
            print("   \u2139\ufe0f  Nessuna sitemap o nessun URL HTML trovato. Scansione classica.")

    # -----------------------------------------------------------------------
    # MODULO IMMAGINI — Sistema avanzato anti-miss (Wix-aware)
    # -----------------------------------------------------------------------

    @staticmethod
    def _is_wix_media_hash(filename: str) -> bool:
        """Verifica se il nome file assomiglia a un hash reale di Wix CDN."""
        name = Path(filename).stem
        if "~mv" in name:
            return True
        if len(name) == 32 and all(c in "0123456789abcdefABCDEF" for c in name):
            return True
        if "_" in name:
            parts = name.split("_")
            if len(parts) >= 2 and len(parts[1]) >= 8:
                return True
        return False

    @staticmethod
    def _extract_wix_filename(raw_url: str, normalized_url: str) -> str:
        """
        Estrae un nome file parlante da un URL Wix se presente (es. alla fine di /v1/fill/...),
        altrimenti ricava il nome dal path normalizzato.
        """
        parsed_raw = urlparse(raw_url)
        path_parts = parsed_raw.path.split('/')
        if len(path_parts) > 1:
            last_part = unquote(path_parts[-1]).split('?')[0]
            ext = Path(last_part).suffix.lower()
            if ext in IMAGE_EXTENSIONS and len(last_part) >= 3:
                return last_part

        # Fallback al path normalizzato
        parsed_norm = urlparse(normalized_url)
        return unquote(Path(parsed_norm.path).name).split('?')[0]

    @staticmethod
    def _normalize_wix_url(url: str) -> str:
        """
        Normalizza gli URL Wix CDN rimuovendo i parametri di ridimensionamento
        per ottenere l'immagine alla risoluzione massima/originale.
        Agisce SOLO su URL di domini Wix (wixstatic.com, wixmp.com).
        URL di altri CDN vengono lasciati invariati (solo query string rimossa).

        Esempi Wix:
          .../media/abc123~mv2.jpg/v1/fill/w_800,h_600,al_c,q_80/.../img.jpg
          → .../media/abc123~mv2.jpg
        """
        # Applica normalizzazione CDN SOLO a domini Wix noti
        WIX_CDN_DOMAINS = (
            "wixstatic.com",
            "wixmp.com",
            "wix-image.com",
        )
        parsed = urlparse(url)
        if any(d in parsed.netloc for d in WIX_CDN_DOMAINS):
            # Pattern: /media/HASH[~mv2][_desc].EXT/v1/...
            wix_match = re.match(
                r"(https?://[^/]+/(?:media|shapes)/[^/]+\.[a-zA-Z0-9]+)(?:/v1/.*)?",
                url, re.IGNORECASE
            )
            if wix_match:
                clean_path = wix_match.group(1)
                # Rimuove il suffisso responsive di Wix come ~mv2_1920.jpg -> ~mv2.jpg
                clean_path = re.sub(r"(~mv\d+)(?:_\d+)(\.[a-zA-Z0-9]+)$", r"\1\2", clean_path, flags=re.IGNORECASE)
                return clean_path

        # Per tutti gli altri URL: rimuove solo query string e fragment
        return url.split("?")[0].split("#")[0]

    @staticmethod
    def _parse_srcset_max(srcset: str) -> str:
        """
        Analizza un attributo srcset e restituisce l'URL con la risoluzione
        più alta (descriptor 'w' o 'x' più grande).
        Formato: "url1 800w, url2 1200w, url3 2x"
        """
        candidates = []
        for part in srcset.split(","):
            part = part.strip()
            if not part:
                continue
            tokens = part.split()
            if not tokens:
                continue
            src = tokens[0]
            # Descriptor: "1200w" → 1200, "2x" → 200 (peso relativo)
            descriptor = 0
            if len(tokens) > 1:
                desc = tokens[1].lower()
                try:
                    if desc.endswith("w"):
                        descriptor = int(desc[:-1])
                    elif desc.endswith("x"):
                        descriptor = int(float(desc[:-1]) * 100)
                except ValueError:
                    pass
            candidates.append((descriptor, src))
        if not candidates:
            return ""
        # Restituisce l'URL con il descriptor più alto
        return max(candidates, key=lambda c: c[0])[1]

    def _trigger_gallery_interactions(self, page: "Page") -> None:
        """
        Interagisce con carousel, gallery e slider tipici di Wix e altri
        builder per far caricare tutte le slide/immagini.
        Clicca i pulsanti "next" fino a esaurimento o max 20 volte per widget.
        """
        # Selettori comuni per pulsanti carousel/gallery (Wix, Slick, Swiper, ecc.)
        next_selectors = [
            # Wix gallery navigator
            "[data-testid='next-navigation-button']",
            "[aria-label='next']",
            "[aria-label='Next']",
            "[aria-label='Avanti']",
            # Wix SlideShow
            "[data-hook='next-button']",
            ".next-button",
            ".slick-next",
            ".swiper-button-next",
            ".owl-next",
            ".flickity-button-icon + .flickity-prev-next-button.next",
            "button.next",
            "a.next",
            # Wix Accordion / Tabs
            "[data-testid='tab-label']",
        ]

        for selector in next_selectors:
            try:
                buttons = page.query_selector_all(selector)
                for btn in buttons:
                    # Clicca fino a 20 volte per coprire tutte le slide
                    for _ in range(20):
                        try:
                            if btn.is_visible():
                                btn.click(timeout=500)
                                page.wait_for_timeout(300)
                            else:
                                break
                        except Exception:
                            break
            except Exception:
                pass

        # Apre anche eventuali accordion (tab nascosti che contengono immagini)
        try:
            accordion_items = page.query_selector_all(
                "[data-testid='accordionItem'], .accordion-item, details"
            )
            for item in accordion_items:
                try:
                    item.click(timeout=300)
                    page.wait_for_timeout(200)
                except Exception:
                    pass
        except Exception:
            pass

    def _extract_images_from_scripts(self, html_content: str, page_url: str) -> set:
        """
        Estrae URL immagine direttamente dai tag <script> e attributi JSON della pagina.
        Wix e altri builder incorporano i dati delle gallery in:
          - wix-warmup-data (ProGallery: campo "mediaUrl" e "name")
          - data-image-info (wixui-gallery: elemento <wow-image>)
          - JSON embedded in script generici
        """
        found = set()

        # ── Pattern regex generici per JSON/JS ─────────────────────────────
        patterns = [
            # Wix media URI relativa — campo "uri"
            r'["\']uri["\']\s*:\s*["\']([a-zA-Z0-9_~%]+\.(?:jpg|jpeg|png|webp|gif|svg))["\']',
            # Wix ProGallery — campo "mediaUrl" (NB: usa solo hash relativo, senza dominio)
            r'"mediaUrl"\s*:\s*"([a-zA-Z0-9_~%]+\.(?:jpg|jpeg|png|webp|gif|svg))"',
            # Wix ProGallery — campo "name" (metaData.name)
            r'"name"\s*:\s*"([a-zA-Z0-9_~%]{10,}\.(?:jpg|jpeg|png|webp|gif|svg))"',
            # URL completi in JSON
            r'"(https?://[^"]+\.(?:jpg|jpeg|png|webp|gif|svg))(?:["?#])',
            # Wix staticmedia URL tra apici singoli
            r"'(https?://static\.wixstatic\.com/media/[^']+\.(?:jpg|jpeg|png|webp|gif|svg))'",
        ]

        for pattern in patterns:
            matches = re.findall(pattern, html_content, re.IGNORECASE)
            for m in matches:
                if m.startswith("http"):
                    found.add(m.split("?")[0])
                else:
                    # Filtra friendly names non risolvibili sul CDN Wix
                    if self._is_wix_media_hash(m):
                        cdn = f"https://static.wixstatic.com/media/{m}"
                        found.add(cdn)

        # ── Parsing strutturato wix-warmup-data (ProGallery) ───────────────
        # Questo cattura le immagini di tutte le slide, incluse quelle non
        # renderizzate nel DOM statico (fuori dalla viewport iniziale).
        found |= self._extract_wix_warmup_gallery_images(html_content)

        # ── Parsing data-image-info su <wow-image> (wixui-gallery) ─────────
        # Elemento usato dalla griglia paginata Wix; contiene JSON HTML-escaped
        # con il campo "uri" dell'immagine originale ad alta risoluzione.
        import html as _html
        for data_info_raw in re.findall(
            r'<wow-image[^>]+data-image-info="([^"]+)"', html_content, re.IGNORECASE
        ):
            try:
                decoded = _html.unescape(data_info_raw)
                uri_m = re.search(
                    r'"uri"\s*:\s*"([a-zA-Z0-9_~%]+\.(?:jpg|jpeg|png|webp|gif|svg))"',
                    decoded, re.IGNORECASE
                )
                if uri_m:
                    found.add(f"https://static.wixstatic.com/media/{uri_m.group(1)}")
            except Exception:
                pass

        return found

    def _extract_wix_warmup_gallery_images(self, html_content: str) -> set:
        """
        Parsa il blocco JSON <script id="wix-warmup-data"> e il blocco
        <script id="wix-props"> della pagina per estrarre tutte le immagini
        delle ProGallery Wix (app 14271d6f-…), incluse quelle non presenti
        nel DOM statico (slide non visibili nella viewport al caricamento).

        Struttura warmup:
          appsWarmupData
            └── "14271d6f-ba62-d045-549b-ab972ae1f70e"  (ProGallery App ID)
                  └── "<compId>_galleryData"
                        └── items[]
                              ├── mediaUrl: "HASH~mv2.jpg"  ← URI relativa
                              └── metaData.name: "HASH~mv2.jpg"
        """
        found = set()
        WIX_PRO_GALLERY_APP_ID = "14271d6f-ba62-d045-549b-ab972ae1f70e"

        # Cerca tutti i blocchi script JSON candidati
        script_blocks = re.findall(
            r'<script[^>]*type="application/json"[^>]*>(.*?)</script>'
            r'|<script[^>]+id="wix-warmup-data"[^>]*>(.*?)</script>'
            r'|<script[^>]+id="wix-props"[^>]*>(.*?)</script>',
            html_content, re.DOTALL | re.IGNORECASE
        )

        def _walk_for_media(obj):
            """Visita ricorsivamente il JSON e raccoglie mediaUrl/name/uri Wix."""
            if isinstance(obj, dict):
                for key, val in obj.items():
                    if key in ("mediaUrl", "uri") and isinstance(val, str):
                        if re.match(r'.+\.(?:jpg|jpeg|png|webp|gif|svg)$', val, re.I):
                            if not val.startswith("http"):
                                found.add(f"https://static.wixstatic.com/media/{val}")
                            else:
                                found.add(val.split("?")[0])
                    elif key == "name" and isinstance(val, str):
                        # "name" può anche essere un titolo testuale; accetta solo
                        # se ha il formato HASH~mv2.EXT (almeno 10 chars + ~mv2)
                        if re.match(r'[a-zA-Z0-9_]{10,}~mv2\.(?:jpg|jpeg|png|webp|gif|svg)$', val, re.I):
                            found.add(f"https://static.wixstatic.com/media/{val}")
                    elif isinstance(val, (dict, list)):
                        _walk_for_media(val)
            elif isinstance(obj, list):
                for item in obj:
                    _walk_for_media(item)

        for groups in script_blocks:
            # re.findall con più gruppi → tuple; unisci tutto
            content = "".join(g for g in groups if g)
            if not content.strip():
                continue
            # Ottimizzazione: salta script che non contengono dati gallery
            if "mediaUrl" not in content and "galleryData" not in content:
                continue
            try:
                data = json.loads(content)
                # Path strutturato: appsWarmupData → ProGallery App → *_galleryData
                apps_data = data.get("appsWarmupData", {})
                pg_app = apps_data.get(WIX_PRO_GALLERY_APP_ID, {})
                for key, val in pg_app.items():
                    if key.endswith("_galleryData") and isinstance(val, dict):
                        _walk_for_media(val)
                # Walk generico per catturare strutture non standard
                _walk_for_media(data.get("platform", {}))
            except Exception:
                # JSON non valido: usa fallback regex (già coperto dai pattern sopra)
                pass

        return found

    def _setup_resource_blocking(self, page: "Page") -> None:
        """
        Blocca risorse non necessarie per velocizzare il caricamento.
        NON interferisce con immagini (resource_type='image') che devono
        essere catturate dall'event handler 'response'.
        Blocca solo: media (video), websocket, eventsource, e tracking URL.
        """
        def _route_handler(route):
            req = route.request
            rt  = req.resource_type
            # Blocca solo tipi non-immagine non necessari
            if rt in BLOCKED_RESOURCE_TYPES:   # media, websocket, eventsource, manifest
                route.abort()
                return
            # Blocca pattern analytics/ads (NON blocca image CDN)
            url_lower = req.url.lower()
            if any(
                pattern in url_lower
                for pattern in [
                    "google-analytics", "googletagmanager", "facebook.net",
                    "doubleclick", "hotjar", "intercom", "crisp.chat",
                    "/gtag/", "fbevents", "clarity.ms",
                ]
            ):
                route.abort()
                return
            route.continue_()

        try:
            page.route("**/*", _route_handler)
        except Exception:
            pass

    def _process_page(self, url: str, context: BrowserContext) -> None:
        """
        Processa una singola pagina:
          - Blocking risorse inutili per caricamenti più veloci
          - Network interception per catturare OGNI immagine caricata dal browser
          - Scroll progressivo ottimizzato (step 600px, wait 80ms)
          - Interazione con carousel/gallery Wix
          - Download immagini in parallelo con ThreadPoolExecutor (15 worker)
        NOTA: Playwright sync API NON è thread-safe. Questo metodo gira sempre
              nel thread principale. Solo i download HTTP usano thread multipli.
        """
        self.visited_urls.add(url)
        page_name = self._get_page_name(url)
        print(f"\n🚀 [{len(self.visited_urls)}/{MAX_PAGES}] Pagina: {page_name}")
        print(f"   🔗 {url}")

        # Cartella immagini specifica per questa pagina
        page_images_dir = self.images_dir / page_name

        # ── Network Interception: usa RESPONSE (non request) ─────────────────
        # 'response' cattura URL reali dopo redirect, solo immagini con HTTP 200/206
        # Evita il conflitto tra page.route() e page.on('request')
        intercepted_image_urls: set = set()
        intercepted_font_urls: set = set()

        def _on_response(response):
            try:
                if response.status in (200, 206):
                    res_type = response.request.resource_type
                    if res_type == "image":
                        with self._lock:
                            intercepted_image_urls.add(response.url)
                    elif res_type == "font":
                        with self._lock:
                            intercepted_font_urls.add(response.url)
            except Exception:
                pass

        page: Page = context.new_page()
        page.on("response", _on_response)

        # Blocca risorse inutili PRIMA della navigazione (analytics, video, ecc.)
        self._setup_resource_blocking(page)

        try:
            # ── Navigazione con retry per DNS transitori ─────────────────────
            # Chromium a volte non risolve hostname che il DNS di sistema gestisce.
            # Strategia: 3 tentativi con delay crescente, alternando hostname.
            import time as _nav_time

            parsed_nav = urlparse(url)
            host = parsed_nav.netloc.split(":")[0]
            alt_host = host[4:] if host.startswith("www.") else "www." + host
            alt_netloc = alt_host
            if ":" in parsed_nav.netloc:
                alt_netloc = f"{alt_host}:{parsed_nav.netloc.split(':')[1]}"
            alt_url = parsed_nav._replace(netloc=alt_netloc).geturl()

            # Lista tentativi: URL originale, alternativo, originale con delay
            nav_attempts = [
                (url, "load", 30000),
                (alt_url, "load", 30000),
                (url, "domcontentloaded", 20000),
                (alt_url, "domcontentloaded", 20000),
            ]

            nav_success = False
            for attempt_idx, (try_nav_url, wait_until, timeout) in enumerate(nav_attempts):
                try:
                    page.goto(try_nav_url, wait_until=wait_until, timeout=timeout)
                    if try_nav_url != url:
                        url = try_nav_url
                        print(f"   ℹ️  DNS fallback: {url}")
                    if wait_until == "domcontentloaded":
                        page.wait_for_timeout(1500)
                    nav_success = True
                    break
                except Exception as nav_err:
                    is_dns = "ERR_NAME_NOT_RESOLVED" in str(nav_err)
                    if attempt_idx < len(nav_attempts) - 1:
                        if is_dns:
                            # Pausa progressiva prima del prossimo tentativo
                            delay = 2 * (attempt_idx + 1)
                            print(f"   ⏳ DNS non risolto, riprovo tra {delay}s...")
                            _nav_time.sleep(delay)
                        # Per timeout o altri errori, continuiamo con il prossimo tentativo della lista
                        continue
                    else:
                        msg = f"Impossibile caricare: {nav_err}"
                        print(f"   ❌ {msg}")
                        logger.error(msg, exc_info=True)
                        self.stats["pages_failed"] += 1
                        return

            if not nav_success:
                msg = f"Tutti i tentativi di navigazione falliti per {url}"
                print(f"   ❌ {msg}")
                logger.error(msg)
                self.stats["pages_failed"] += 1
                return


            # ── Scroll progressivo ottimizzato ──────────────────────────────
            # Step 600px (era 400px) e wait 80ms (era 150ms) = ~2.5x più veloce
            try:
                total_height: int = page.evaluate("document.body.scrollHeight")
                step    = 600
                current = 0
                while current < total_height:
                    page.evaluate(f"window.scrollTo(0, {current})")
                    page.wait_for_timeout(80)
                    current += step
                    total_height = page.evaluate("document.body.scrollHeight")
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(800)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(300)
            except Exception:
                pass

            # ── Interazione gallery/carousel Wix ─────────────────────────────
            self._trigger_gallery_interactions(page)
            page.wait_for_timeout(500)

            # ── Acquisizione HTML finale ─────────────────────────────────────
            html_content = page.content()
            soup = BeautifulSoup(html_content, "html.parser")

            # --- Moduli di estrazione ---
            self._extract_internal_links(soup, url)   # Link interni → coda
            with self._lock:
                intercepted_fonts_snapshot = set(list(intercepted_font_urls))
            self._scrape_fonts(page, url, intercepted_fonts_snapshot)  # Font → TTF/OTF
            self._scrape_documents(page, url, page_name)  # PDF, DOC, ecc.
            self._save_text(BeautifulSoup(html_content, "html.parser"), page_name)

            # ── Wix Stores: estrai prodotti e inietta pagine prodotto ────────
            # Deve girare PRIMA di _process_product per garantire che i prodotti
            # Wix Stores (rilevati dal warmup-data) abbiano priorità sul fallback euristico.
            self._extract_wix_store_products(html_content, url)
            self._inject_wix_store_product_urls(html_content, url)

            # ── Fallback prodotto (JSON-LD / OG / DOM) per non-Wix-Stores ──
            self._process_product(soup, url)

            # Snapshot thread-safe di intercepted_image_urls per evitare modifiche durante l'unione
            with self._lock:
                intercepted_snapshot = set(list(intercepted_image_urls))

            # ── Raccolta immagini da tutte le fonti ────────────────────────
            dom_img    = self._collect_dom_images(page, url, html_content)
            script_img = self._extract_images_from_scripts(html_content, url)
            all_imgs   = dom_img | script_img | intercepted_snapshot

            # Log diagnostico per debug (rimuovibile in produzione)
            if script_img:
                warmup_only = script_img - dom_img - intercepted_snapshot
                if warmup_only:
                    print(f"   🎨 Wix gallery (warmup-data): {len(warmup_only)} immagini extra")

            # FASE 1 — Download via Playwright per TUTTE le immagini.
            # Playwright usa lo stesso contesto HTTP (cookie di sessione Wix, ecc.)
            # quindi bypassa protezioni CDN basate su cookie/sessione.
            # Le immagini da warmup-data e DOM che non sono state intercettate
            # hanno bisogno del contesto browser per evitare 404 dal CDN Wix.
            pw_downloaded = self._playwright_download_images(
                page, all_imgs, page_images_dir, url
            )
            if pw_downloaded:
                self.stats["images_downloaded"] += pw_downloaded
                print(f"   🖼️  Playwright download: {pw_downloaded} immagini")

            # FASE 2 — Download HTTP parallelo per le immagini non ancora scaricate
            self._download_all_images(all_imgs, url, page_images_dir)

        except Exception as exc:
            msg = f"Errore su [{url}]: {exc}"
            print(f"   ❌ {msg}")
            logger.error(msg, exc_info=True)

        finally:
            page.close()

    def _playwright_download_images(
        self, page: "Page", url_set: set, dest_dir: Path, page_url: str
    ) -> int:
        """
        Scarica immagini usando il contesto HTTP di Playwright (page.request).
        Questo metodo bypassa qualsiasi protezione CDN basata su cookie/sessione
        (es. Wix, Squarespace) perché usa lo stesso contesto del browser.

        Deve girare nel thread principale (Playwright non è thread-safe).
        Restituisce il numero di file scaricati con successo.
        """
        downloaded_count = 0
        seen_names: set = set()

        for raw_url in url_set:
            try:
                if not raw_url or "http" not in raw_url:
                    continue

                # URL originale senza query string
                clean_url = urljoin(page_url, raw_url).split("?")[0]

                # URL normalizzato (per Wix: base senza /v1/fill/)
                normalized = self._normalize_wix_url(clean_url)

                # Verifica che abbia un'estensione immagine o sia un CDN noto
                parsed_path = urlparse(normalized).path
                ext = Path(parsed_path).suffix.lower()
                CDN_NO_EXT = ("wixstatic.com", "cloudinary.com", "imgix.net",
                              "unsplash.com", "shopify.com", "squarespace-cdn.com")
                if ext not in IMAGE_EXTENSIONS and not any(c in normalized for c in CDN_NO_EXT):
                    continue

                # Salta URL Wix non validi (friendly names rimasti nel path)
                if "static.wixstatic.com/media/" in normalized:
                    fname = Path(urlparse(normalized).path).name
                    if not self._is_wix_media_hash(fname):
                        continue

                # Estrarre nome file parlante
                parsed_path = urlparse(normalized).path
                ext = Path(parsed_path).suffix.lower() or ".jpg"
                img_name = self._extract_wix_filename(raw_url, normalized)
                if not img_name:
                    img_name = f"img_{abs(hash(normalized)) % 99999:05d}{ext}"
                else:
                    img_name = self._sanitize_name(Path(img_name).stem) + Path(img_name).suffix

                if not Path(img_name).suffix:
                    img_name += ext

                # Anti-duplicato per nome nella pagina
                if img_name in seen_names:
                    stem   = Path(img_name).stem
                    suffix = Path(img_name).suffix
                    img_name = f"{stem}_{abs(hash(normalized)) % 9999}{suffix}"
                seen_names.add(img_name)

                dest_path = dest_dir / img_name

                # Se già scaricato in sessione, copia il file locale per questa pagina
                with self._lock:
                    if normalized in self.downloaded_files:
                        existing_path = self.downloaded_files[normalized]
                        if existing_path.exists() and not dest_path.exists():
                            import shutil
                            try:
                                dest_path.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(existing_path, dest_path)
                                downloaded_count += 1
                                self.stats["images_cached"] += 1
                            except Exception:
                                pass
                        else:
                            self.stats["images_cached"] += 1
                        continue

                if dest_path.exists():
                    with self._lock:
                        self.downloaded_files[normalized] = dest_path
                    continue

                # Download via Playwright (raw_url preserva i parametri originali)
                # che il browser ha già usato con successo
                try:
                    # Tentativo 1: URL normalizzato (base, max risoluzione)
                    resp = page.request.get(
                        normalized,
                        headers={"Referer": page_url},
                        timeout=15000,
                    )
                    # Tentativo 2: URL raw originale (con parametri CDN)
                    if not resp.ok and raw_url != normalized:
                        resp = page.request.get(
                            raw_url,
                            headers={"Referer": page_url},
                            timeout=15000,
                        )
                    # Tentativo 3: URL fill costruito per Wix CDN
                    if not resp.ok and "wixstatic.com/media/" in normalized:
                        wix_fname = Path(urlparse(normalized).path).name
                        fill_url = (
                            f"{normalized}/v1/fill/"
                            f"w_1920,h_1080,al_c,q_90,usm_0.66_1.00_0.01,"
                            f"enc_avif,quality_auto/{wix_fname}"
                        )
                        resp = page.request.get(
                            fill_url,
                            headers={"Referer": page_url},
                            timeout=15000,
                        )
                    if resp.ok:
                        body = resp.body()
                        if body and len(body) > 100:  # Scarta risposte vuote/errori
                            dest_path.parent.mkdir(parents=True, exist_ok=True)
                            with open(dest_path, "wb") as f:
                                f.write(body)
                            
                            # Comprimi il file se supera i 25 MB in automatico
                            self._compress_document_if_needed(dest_path)

                            with self._lock:
                                self.downloaded_files[normalized] = dest_path
                            downloaded_count += 1
                except Exception:
                    pass  # Fallback all'HTTP classico in Fase 2

            except Exception:
                pass

        return downloaded_count

    def _collect_internal_links(self, soup: BeautifulSoup, current_url: str) -> set:
        """Raccoglie i link interni senza modificare lo stato (thread-safe helper)."""
        links = set()
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            full_url = urljoin(current_url, href)
            parsed   = urlparse(full_url)
            clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
            url_domain = parsed.netloc.replace("www.", "")
            skip_exts = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".zip", ".doc")
            if (
                url_domain == self.domain
                and not any(clean_url.lower().endswith(e) for e in skip_exts)
                and parsed.scheme in ("http", "https")
            ):
                links.add(clean_url)
        return links

    def _process_product_safe(self, soup: BeautifulSoup, url: str) -> None:
        """
        Versione thread-safe di _process_product.
        Devia le scritture su una lista temporanea, poi fa il merge
        sulla lista condivisa sotto lock, garantendo zero race condition.
        """
        # Sostituisce temporaneamente la lista condivisa con una locale
        with self._lock:
            original_list = self.detected_products
            self.detected_products = []  # Buffer temporaneo

        try:
            # _process_product ora scrive sul buffer temporaneo (nessun altro thread lo vede)
            self._process_product(soup, url)
            new_items = self.detected_products  # Prodotti appena trovati
        finally:
            # Ripristina SEMPRE la lista originale
            with self._lock:
                self.detected_products = original_list

        # Merge thread-safe: aggiunge solo prodotti non già presenti
        if new_items:
            with self._lock:
                existing_names = {p["NAME"] for p in self.detected_products}
                for item in new_items:
                    if item["NAME"] not in existing_names:
                        self.detected_products.append(item)
                        existing_names.add(item["NAME"])
                        print(f"   🛍️  Prodotto: {item['NAME']}"
                              f"{'  →  €' + item['PRICE'] if item['PRICE'] else ''}")

    def _collect_dom_images(self, page: "Page", page_url: str, html_content: str) -> set:
        """
        Raccoglie tutti gli URL immagine presenti nel DOM tramite JavaScript.
        Copre: src, srcset (max res), data-src, data-lazy, data-bg,
        background-image (computed), attributi Wix personalizzati,
        elementi <source> nei <picture>, e URL da attributi data-* generici.
        """
        try:
            raw_urls: list = page.evaluate("""
            () => {
                const urls = new Set();

                // ── Helper: aggiunge URL validi (filtra data: URI) ──────────
                const add = (u) => {
                    if (u && typeof u === 'string' && u.startsWith('http')
                        && !u.startsWith('data:')) {
                        urls.add(u.trim());
                    }
                };

                // ── 1. Tag <img>: tutti gli attributi possibili ─────────────
                document.querySelectorAll('img').forEach(img => {
                    add(img.src);
                    add(img.currentSrc);           // Risoluzione effettiva
                    add(img.getAttribute('data-src'));
                    add(img.getAttribute('data-lazy-src'));
                    add(img.getAttribute('data-lazy'));
                    add(img.getAttribute('data-original'));
                    add(img.getAttribute('data-image'));
                    add(img.getAttribute('data-bg'));
                    add(img.getAttribute('data-background'));
                    add(img.getAttribute('data-hi-res-src'));
                    // Wix attributi custom
                    add(img.getAttribute('data-pin-media'));
                    // srcset: tutti i candidati
                    const srcset = img.getAttribute('srcset') || img.getAttribute('data-srcset');
                    if (srcset) {
                        srcset.split(',').forEach(s => {
                            const u = s.trim().split(/\\s+/)[0];
                            add(u);
                        });
                    }
                });

                // ── 2. Tag <source> in <picture> ───────────────────────────
                document.querySelectorAll('source').forEach(src => {
                    const srcset = src.getAttribute('srcset') || '';
                    srcset.split(',').forEach(s => {
                        const u = s.trim().split(/\\s+/)[0];
                        add(u);
                    });
                    add(src.getAttribute('src'));
                    add(src.getAttribute('data-srcset'));
                });

                // ── 3. Background-image (computed style su TUTTI gli elementi)
                document.querySelectorAll('*').forEach(el => {
                    // Computed style
                    const bg = window.getComputedStyle(el).backgroundImage;
                    if (bg && bg !== 'none') {
                        const matches = bg.matchAll(/url\\(["']?([^"'\\)]+)["']?\\)/g);
                        for (const m of matches) add(m[1]);
                    }
                    // Inline style background-image
                    const inlineStyle = el.getAttribute('style') || '';
                    const inlineMatches = inlineStyle.matchAll(/background(?:-image)?\\s*:\\s*url\\(["']?([^"'\\)]+)["']?\\)/gi);
                    for (const m of inlineMatches) add(m[1]);
                    // Attributi data-* che contengono URL immagine
                    for (const attr of el.attributes) {
                        if (attr.name.startsWith('data-') && attr.value.match(/https?:\/\/.*\.(?:jpg|jpeg|png|webp|gif|svg)/i)) {
                            add(attr.value.split('?')[0]);
                        }
                    }
                });

                // ── 4. Wix-specific: elementi <wix-image> e attributi Wix ──
                document.querySelectorAll('[data-testid="image"], wix-image, [class*="wixui-image"]').forEach(el => {
                    add(el.getAttribute('src'));
                    add(el.getAttribute('data-src'));
                    // Wix serialized props
                    const props = el.getAttribute('data-props') || el.getAttribute('data-image-info') || '';
                    const uriMatch = props.match(/["']?uri["']?\\s*:\\s*["']([^"']+)["']/);
                    if (uriMatch) add('https://static.wixstatic.com/media/' + uriMatch[1]);
                });

                // ── 5. Wix Gallery JSON embedded in data-props ────────────
                document.querySelectorAll('[data-props]').forEach(el => {
                    try {
                        const props = JSON.parse(el.getAttribute('data-props'));
                        const findImages = (obj) => {
                            if (!obj || typeof obj !== 'object') return;
                            for (const key of Object.keys(obj)) {
                                if (key === 'uri' && typeof obj[key] === 'string'
                                    && obj[key].match(/\\.(jpg|jpeg|png|webp|gif|svg)$/i)) {
                                    add('https://static.wixstatic.com/media/' + obj[key]);
                                } else if (key.toLowerCase().includes('src') || key.toLowerCase().includes('image')) {
                                    if (typeof obj[key] === 'string') add(obj[key]);
                                }
                                if (Array.isArray(obj[key])) obj[key].forEach(findImages);
                                else if (typeof obj[key] === 'object') findImages(obj[key]);
                            }
                        };
                        findImages(props);
                    } catch(e) {}
                });

                return [...urls];
            }
            """)
        except Exception as exc:
            msg = f"Errore raccolta DOM immagini: {exc}"
            print(f"   ⚠️  {msg}")
            logger.warning(msg, exc_info=True)
            raw_urls = []


        # Raccoglie anche i srcset analizzando l'HTML con BeautifulSoup per sicurezza
        soup = BeautifulSoup(html_content, "html.parser")
        extra_from_bs4 = set()
        for tag in soup.find_all(["img", "source"]):
            srcset = tag.get("srcset") or tag.get("data-srcset", "")
            if srcset:
                best = self._parse_srcset_max(srcset)
                if best:
                    extra_from_bs4.add(urljoin(page_url, best))

        return set(raw_urls) | extra_from_bs4

    def _download_all_images(self, url_set: set, page_url: str, dest_dir: Path) -> None:
        """
        Download parallelo di tutte le immagini (DOWNLOAD_WORKERS thread simultanei).
        - Normalizza URL Wix CDN alla sorgente originale (massima risoluzione)
        - Cache globale cross-pagina: non riscarica URL già processati
        - Anti-duplicato per nome file nella stessa pagina
        """
        # --- Fase 1: preparazione lista URL da scaricare ---
        tasks: list = []          # (full_url, img_name, dest_dir)
        seen_names: set = set()   # Anti-duplicato per nome nella pagina corrente

        # CDN che servono immagini senza estensione nel path
        CDN_NO_EXT_PATTERNS = (
            "wixstatic.com/media/",
            "cloudinary.com/",
            "imgix.net/",
            "images.unsplash.com/",
            "cdn.shopify.com/",
            "images.squarespace-cdn.com/",
            "imagekit.io/",
            "res.cloudinary.com/",
            "media.graphassets.com/",
        )

        for raw_url in url_set:
            try:
                if not raw_url or "http" not in raw_url:
                    continue

                # URL originale (senza query string, ma con path /v1/fill/ intatto)
                original_url = urljoin(page_url, raw_url).split("?")[0]

                # URL normalizzato (per Wix: rimuove /v1/fill/ per ottenere l'originale)
                full_url = self._normalize_wix_url(original_url)

                # Salta URL Wix non validi (friendly names rimasti nel path)
                if "static.wixstatic.com/media/" in full_url:
                    fname = Path(urlparse(full_url).path).name
                    if not self._is_wix_media_hash(fname):
                        continue

                # Verifica estensione sull'URL normalizzato
                parsed_path = urlparse(full_url).path
                ext = Path(parsed_path).suffix.lower()

                if ext not in IMAGE_EXTENSIONS:
                    # Accetta se è un CDN noto che non mette estensione
                    if not any(p in full_url for p in CDN_NO_EXT_PATTERNS):
                        continue
                    ext = ".jpg"  # Estensione fallback per CDN senza ext

                # Nome file
                img_name = self._extract_wix_filename(raw_url, full_url)
                if not img_name:
                    img_name = f"img_{abs(hash(full_url)) % 99999:05d}{ext or '.jpg'}"
                else:
                    img_name = self._sanitize_name(Path(img_name).stem) + Path(img_name).suffix

                if not Path(img_name).suffix:
                    img_name = img_name + (ext or ".jpg")

                # Anti-duplicato per nome nella pagina
                if img_name in seen_names:
                    stem   = Path(img_name).stem
                    suffix = Path(img_name).suffix or ext
                    img_name = f"{stem}_{abs(hash(full_url)) % 9999}{suffix}"

                seen_names.add(img_name)

                dest_path = dest_dir / img_name

                # Se già scaricato in sessione, copia il file locale per questa pagina
                with self._lock:
                    if full_url in self.downloaded_files:
                        existing_path = self.downloaded_files[full_url]
                        if existing_path.exists() and not dest_path.exists():
                            import shutil
                            try:
                                dest_path.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(existing_path, dest_path)
                                self.stats["images_cached"] += 1
                            except Exception:
                                pass
                        else:
                            self.stats["images_cached"] += 1
                        continue

                # ── Costruisce la catena di fallback URL ─────────────────────
                # Wix: se l'URL normalizzato dà 404, proviamo:
                #   1. L'URL originale con parametri /v1/fill/ (quello che il browser usa)
                #   2. Una versione fill a risoluzione standard 1920x1080
                fallback_urls = []
                if "wixstatic.com/media/" in full_url:
                    fname = Path(urlparse(full_url).path).name
                    if original_url != full_url:
                        fallback_urls.append(original_url)  # URL con /v1/fill/ originale
                    # Versione fill a risoluzione alta come ulteriore fallback
                    fill_url = (
                        f"{full_url}/v1/fill/"
                        f"w_1920,h_1080,al_c,q_90,usm_0.66_1.00_0.01,"
                        f"enc_avif,quality_auto/{fname}"
                    )
                    fallback_urls.append(fill_url)

                tasks.append((full_url, img_name, fallback_urls))

                # Prenota subito nella cache globale con percorso provvisorio (evita race condition)
                with self._lock:
                    self.downloaded_files[full_url] = dest_path

            except Exception:
                pass

        if not tasks:
            print(f"   🖼️  Immagini scaricate: 0 (tutte in cache o già elaborate)")
            return

        # --- Fase 2: download parallelo con ThreadPoolExecutor ---
        downloaded_count = 0

        def _do_download(args):
            url, name, fallbacks = args
            success = self._download_file(
                url, dest_dir, name,
                referer=page_url,
                fallback_urls=fallbacks,
            )
            if success:
                with self._lock:
                    self.downloaded_files[url] = dest_dir / name
            return success

        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
            futures = {pool.submit(_do_download, task): task for task in tasks}
            for future in as_completed(futures, timeout=120):
                url_done, name_done, _fallbacks = futures[future]
                try:
                    if future.result():
                        downloaded_count += 1
                        self.stats["images_downloaded"] += 1
                    else:
                        # Download fallito: rimuovi dalla cache globale
                        with self._lock:
                            self.downloaded_files.pop(url_done, None)
                        self.stats["images_failed"] += 1
                except Exception as exc:
                    msg = f"Eccezione durante il download parallelo di {name_done}: {exc}"
                    logger.warning(msg, exc_info=True)
                    with self._lock:
                        self.downloaded_files.pop(url_done, None)


        print(f"   🖼️  Immagini scaricate: {downloaded_count}/{len(tasks)}")

    # -----------------------------------------------------------------------
    # ENTRY POINT PRINCIPALE: run()
    # -----------------------------------------------------------------------

    def run(self) -> None:
        """
        Esegue l'intera pipeline di importazione:
          1. Setup cartelle
          2. Caricamento sitemap
          3. Navigazione sequenziale con un unico browser Playwright
             (Playwright sync API non è thread-safe: una pagina alla volta)
          4. Download immagini in PARALLELO per ogni pagina (15 worker HTTP)
          5. Generazione CSV Flazio
        """
        self._setup_directories()

        try:
            print("\n" + "=" * 60)
            print(f"  🎯 FLAZIO IMPORT ASSISTANT — avviato su:")
            print(f"     {self.target_url}")
            print(f"  📁 Output → {self.root_dir}")
            print(f"  ⚡ Download image worker: {DOWNLOAD_WORKERS} | Max pagine: {MAX_PAGES}")
            print("=" * 60 + "\n")

            self._load_sitemap()

            # Un unico browser + un unico context (Playwright sync API è single-thread)
            with sync_playwright() as pw:
                self._playwright = pw
                self._browser = pw.chromium.launch(
                    headless=True,
                    args=["--disable-async-dns"],  # Usa DNS di sistema (più affidabile)
                )
                context: BrowserContext = self._browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1920, "height": 1080},
                )

                try:
                    # Loop sequenziale — visited_urls come anti-ciclo
                    while self.pages_to_crawl and len(self.visited_urls) < MAX_PAGES:
                        next_url = self.pages_to_crawl.pop()
                        if next_url not in self.visited_urls:
                            self._process_page(next_url, context)
                finally:
                    context.close()
                    self._browser.close()

            # Generazione CSV finale
            self._write_csv()

            # Riepilogo finale
            self.stats["pages_ok"] = len(self.visited_urls) - self.stats["pages_failed"]
            self.stats["products_detected"] = len(self.detected_products)

            print("\n" + "=" * 60)
            print("  ✅ COMPLETATO!")
            print(f"  📄 Pagine analizzate : {len(self.visited_urls)}")
            print(f"  🛍️  Prodotti rilevati : {self.stats['products_detected']}")
            print(f"  📁 Cartella output   : {self.root_dir}")
            print("=" * 60 + "\n")
            
            # Scrittura del riepilogo dettagliato all'inizio del file di log
            try:
                site_log_path = self.root_dir / "import_details.txt"
                if site_log_path.exists():
                    with open(site_log_path, "r", encoding="utf-8") as f:
                        old_content = f.read()
                    
                    summary = (
                        "============================================================\n"
                        "               RIEPILOGO DETTAGLIATO SCRAPING               \n"
                        "============================================================\n"
                        f"  📄 Pagine Caricate con Successo: {self.stats['pages_ok']}\n"
                        f"  ⚠️  Pagine Fallite             : {self.stats['pages_failed']}\n"
                        f"  🖼️  Immagini Scaricate         : {self.stats['images_downloaded']}\n"
                        f"  🔄 Immagini Saltate (in cache) : {self.stats['images_cached']}\n"
                        f"  ❌ Immagini Fallite (404/Err)  : {self.stats['images_failed']}\n"
                        f"  🔤 Font Convertiti             : {self.stats['fonts_converted']}\n"
                        f"  ♻️  Font Duplicati (saltati)   : {self.stats['fonts_duplicated']}\n"
                        f"  📄 Documenti Scaricati         : {self.stats['documents_downloaded']}\n"
                        f"  📝 File di Testo Salvati       : {self.stats['texts_saved']}\n"
                        f"  🛍️  Prodotti Rilevati          : {self.stats['products_detected']}\n"
                        "============================================================\n\n"
                        "--- LOG ERRORI E WARNING DI DETTAGLIO ---\n\n"
                    )
                    
                    with open(site_log_path, "w", encoding="utf-8") as f:
                        f.write(summary + old_content)
            except Exception as e:
                print(f"   ⚠️ Impossibile scrivere il riepilogo in import_details.txt: {e}")
                
        finally:
            if hasattr(self, '_site_handler'):
                logger.removeHandler(self._site_handler)
                self._site_handler.close()



# ---------------------------------------------------------------------------
# AVVIO DA TERMINALE
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("      FLAZIO IMPORT ASSISTANT — V11")
    print("      Senior Python & Automation Edition")
    print("=" * 60 + "\n")

    try:
        if len(sys.argv) > 1:
            sito = sys.argv[1].strip()
        else:
            sito = input("🔗 Inserisci l'URL del sito da analizzare: ").strip()
        
        if not sito:
            msg = "URL non fornito. Uscita."
            print(f"❌ {msg}")
            logger.error(msg)
            sys.exit(1)


        assistant = FlazioImportAssistant(sito)
        assistant.run()

    except KeyboardInterrupt:
        print("\n\n⚠️  Operazione interrotta dall'utente.")
        sys.exit(0)