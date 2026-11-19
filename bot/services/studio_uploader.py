"""Playwright ile YouTube Studio üzerinden video yükleme.

YouTube Data API bilinçli olarak KULLANILMAZ (doğrulanmamış projelerden API ile
yüklenen videolar private kilitlenir); yükleme, kalıcı Chrome profiliyle
Studio arayüzünden yapılır ve normal yükleme sayılır.

Selector'lar tek sözlükte toplanmıştır (``SELECTORS``); Studio arayüzü
değişirse yalnızca burası güncellenir. Metin bazlı aramalar Türkçe VE İngilizce
arayüzü destekler (``TEXTS``).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ..config import Config, get_config
from . import db, vnc

logger = logging.getLogger(__name__)

# Aynı anda tek yükleme: ikinci /baslat akışı bu kilit serbest kalmadan
# Studio'ya dokunamaz.
UPLOAD_LOCK = asyncio.Lock()

STUDIO_URL = "https://studio.youtube.com/"
CHANNEL_SWITCHER_URL = "https://www.youtube.com/channel_switcher"

# Gelen doğrulama kodlarını asenkron beklemek için Future haritası (chat_id -> Future)
VERIFICATION_FUTURES: dict[int, asyncio.Future[str]] = {}

# Headless Chromium'un user-agent'ı "HeadlessChrome" içerir; Studio bunu
# "desteklenmeyen tarayıcı" sayar. Headless modda normal Chrome UA kullanılır.
HEADLESS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# --- Tüm selector'lar tek yerde -------------------------------------------
SELECTORS: dict[str, str] = {
    # Studio üst çubuğu / hesap
    "account_button": "#avatar-btn, #account-button",
    "account_menu": "ytcp-account-menu, tp-yt-iron-dropdown ytd-multi-page-menu-renderer",
    "account_name": "ytcp-account-menu #account-name, #account-name",
    "create_button": "ytcp-button#create-icon, #create-icon-button",
    "upload_menu_item": "tp-yt-paper-item#text-item-0",
    # youtube.com/channel_switcher — kanal kartları
    "switcher_item": "ytd-account-item-renderer",
    "switcher_item_name": "#channel-title",
    # Studio sol menüsü — aktif kanalın adı
    "active_channel": "ytcp-navigation-drawer #entity-name, #entity-name",
    # Yükleme diyaloğu
    "upload_dialog": "ytcp-uploads-dialog",
    "file_input": "ytcp-uploads-dialog input[type=file], input[type=file]",
    "dialog_error": "ytcp-uploads-dialog .error-short, ytcp-uploads-dialog .error-details",
    # Ayrıntılar ekranı
    "title_box": "#title-textarea #textbox",
    "description_box": "#description-textarea #textbox",
    "thumbnail_input": "ytcp-thumbnail-uploader input#file-loader, input#file-loader",
    "kids_no_radio": "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']",
    "video_link_anchor": "a.ytcp-video-info, ytcp-video-info a[href*='youtu']",
    # Adım butonları
    "next_button": "#next-button",
    "done_button": "#done-button",
    # Görünürlük
    "public_radio": "tp-yt-paper-radio-button[name='PUBLIC']",
    # İlerleme etiketi (diyalog altı: "Yükleniyor %42..." / "Kontroller tamamlandı")
    "progress_label": "ytcp-video-upload-progress span.progress-label, span.progress-label",
    # Yayınlandı diyaloğu
    "share_dialog": "ytcp-video-share-dialog",
    "share_url": "ytcp-video-share-dialog a#share-url, ytcp-video-share-dialog a[href*='youtu']",
    "share_close": "ytcp-video-share-dialog #close-button",
}

# Türkçe + İngilizce arayüz metinleri (profil diline göre ikisi de denenir)
TEXTS: dict[str, list[str]] = {
    "upload_videos": ["Video yükle", "Videos yükleyin", "Upload videos", "Upload video"],
    "continue_anyway": ["YOUTUBE STUDIO'YA GEÇ", "YouTube Studio'ya geç", "Go to YouTube Studio"],
    "create": ["Oluştur", "Create"],
    "checks_done": ["kontroller tamamlandı", "checks complete"],
    "verify_identity": ["Kimliğinizi doğrulayın", "Verify it's you", "Verify your identity"],
    "publish_anyway": ["Yine de yayınla", "Publish anyway"],
    "upload_done": ["yükleme tamamlandı", "upload complete", "işleniyor", "processing"],
    "uploading": ["yükleniyor", "uploading"],
}

ProgressCallback = Callable[[str], Awaitable[None]]
ScreenshotCallback = Callable[[bytes, str], Awaitable[None]]


class SessionExpiredError(RuntimeError):
    """Google oturumu düşmüş; --login-setup gerekli."""


class StudioUploadError(RuntimeError):
    """Studio akışında bir adım başarısız oldu.

    Attributes:
        step: Takılınan adımın Türkçe adı.
        screenshot: Varsa hata anındaki tam sayfa ekran görüntüsü (PNG).
    """

    def __init__(self, step: str, message: str, screenshot: bytes | None = None) -> None:
        super().__init__(message)
        self.step = step
        self.screenshot = screenshot


async def _human_pause(low: float = 0.5, high: float = 1.5) -> None:
    """İnsan benzeri tempo için küçük rastgele gecikme."""
    await asyncio.sleep(random.uniform(low, high))


_UCID_RE = re.compile(r"UC[\w-]{22}")
_UCID_JSON_RE = re.compile(r'"(?:channelId|externalId)":"(UC[\w-]{22})"')


MANUAL_VERIFY_MINUTES = 10  # kullanıcının uzak ekranda doğrulamayı bitirmesi için süre

_CODE_INPUT ="input[type=tel], input[id=code], input[name=code], input[type=text]"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def _studio_channel_url(ucid: str) -> str:
    return f"{STUDIO_URL}channel/{ucid}"


def _clear_profile_locks(cfg: Config) -> None:
    """Chromium çökmesinden kalan profil kilitlerini temizler (SingletonLock vb.)."""
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_file = cfg.browser_profile_dir / lock_name
        if lock_file.exists() or lock_file.is_symlink():
            try:
                lock_file.unlink()
            except OSError as exc:
                logger.warning("Kilit dosyası %s silinemedi: %s", lock_name, exc)


def _launch_kwargs(cfg: Config) -> dict:
    kwargs: dict = {
        "user_data_dir": str(cfg.browser_profile_dir),
        "headless": cfg.headless,
        "viewport": {"width": 1440, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if cfg.headless:
        kwargs["user_agent"] = HEADLESS_USER_AGENT
    return kwargs


def _is_login_url(url: str) -> bool:
    return "accounts.google.com" in url or "/signin" in url


async def _read_switcher(page: Page) -> list[dict[str, str]]:
    """youtube.com/channel_switcher sayfasındaki kanalları okur: görünen ad + @tanıtıcı."""
    # Studio'nun kendi yönlendirmesi sürerken yapılan geçiş geri alınabiliyor;
    # adres gerçekten kanal seçiciye gelene kadar yeniden denenir.
    for _ in range(3):
        await page.goto(CHANNEL_SWITCHER_URL, wait_until="domcontentloaded")
        await asyncio.sleep(1.0)
        if "channel_switcher" in page.url or _is_login_url(page.url):
            break
    if _is_login_url(page.url):
        raise SessionExpiredError("Google oturumu düşmüş. /giris komutuyla yeniden giriş yapın.")
    items = page.locator(SELECTORS["switcher_item"])
    try:
        await items.first.wait_for(state="visible", timeout=30_000)
    except PlaywrightTimeoutError as exc:
        if _is_login_url(page.url):
            raise SessionExpiredError(
                "Google oturumu düşmüş. /giris komutuyla yeniden giriş yapın.") from exc
        raise StudioUploadError(
            "kanal listesi", "Kanal seçim sayfası (youtube.com/channel_switcher) yüklenemedi."
        ) from exc
    await _human_pause(1.5, 2.5)  # kart verilerinin bağlanması için

    entries: list[dict[str, str]] = []
    for i in range(await items.count()):
        item = items.nth(i)
        name = (await item.locator(SELECTORS["switcher_item_name"]).first.inner_text()).strip()
        handle = await item.evaluate(
            "e => (e.data && e.data.channelHandle && e.data.channelHandle.simpleText) || ''"
        )
        if name:
            entries.append({"name": name, "handle": (handle or "").strip()})
    return entries


async def _resolve_handle(page: Page, handle: str) -> str | None:
    """@tanıtıcıdan kanal kimliğini (UC...) bulur; bulunamazsa None."""
    if not handle.startswith("@"):
        return None
    await page.goto(f"https://www.youtube.com/{handle}", wait_until="domcontentloaded")
    try:
        href = await page.locator('link[rel="canonical"]').first.get_attribute("href", timeout=10_000)
    except Exception:  # noqa: BLE001
        href = None
    match = _UCID_RE.search(href or "")
    if match is not None:
        return match.group(0)
    match = _UCID_JSON_RE.search(await page.content())
    return match.group(1) if match else None


class StudioUploader:
    """YouTube Studio yükleme otomasyonu (kalıcı Chrome profiliyle)."""

    def __init__(self, config: Config | None = None) -> None:
        self._cfg = config or get_config()

    # ------------------------------------------------------------- public

    async def upload(
        self,
        video_path: Path,
        title: str,
        description: str,
        channel_name: str,
        chat_id: int,
        thumbnail_path: Path | None = None,
        tags: list[str] | None = None,
        on_progress: ProgressCallback | None = None,
        on_screenshot: ScreenshotCallback | None = None,
        on_channel_confirm: Callable[[bytes], Any] | None = None,
    ) -> str:
        """Videoyu seçilen kanala yükler, Herkese Açık yayınlar; video linkini döndürür.

        Raises:
            SessionExpiredError: Google oturumu düşmüşse.
            StudioUploadError: Herhangi bir adım başarısız olursa (ekran
                görüntüsüyle birlikte).
        """
        self._chat_id = chat_id
        self._on_progress = on_progress
        self._on_screenshot = on_screenshot
        
        # Chromium çökme sonrası kilit dosyalarını temizle (SingletonLock vb.)
        for lock_name in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            lock_file = self._cfg.browser_profile_dir / lock_name
            if lock_file.exists() or lock_file.is_symlink():
                try:
                    lock_file.unlink()
                    logger.info("Eski kilit dosyası temizlendi: %s", lock_name)
                except Exception as exc:
                    logger.warning("Kilit dosyası %s silinemedi: %s", lock_name, exc)
                    
        step = "tarayıcı başlatma"
        async with async_playwright() as pw:
            launch_kwargs: dict = {
                "user_data_dir": str(self._cfg.browser_profile_dir),
                "headless": self._cfg.headless,
                "viewport": {"width": 1440, "height": 900},
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            if self._cfg.headless:
                launch_kwargs["user_agent"] = HEADLESS_USER_AGENT
            context = await pw.chromium.launch_persistent_context(**launch_kwargs)
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(60_000)
            try:
                step = "oturum kontrolü"
                await self._check_session(page)
                await self._handle_identity_check(page)

                step = "kanal değiştirme"
                await self._ensure_channel(context, page, channel_name)
                await self._handle_identity_check(page)

                if on_channel_confirm:
                    logger.info("Kanal onayı bekleniyor...")
                    approved = await on_channel_confirm(await page.screenshot(full_page=False))
                    if not approved:
                        raise StudioUploadError(
                            "kanal doğrulaması",
                            "Kullanıcı kanal adını hatalı buldu ve yüklemeyi durdurdu."
                        )

                step = "yükleme diyaloğunu açma"
                await self._open_upload_dialog(page)
                await self._handle_identity_check(page)

                step = "video dosyasını verme"
                await self._assert_channel(page, channel_name, "video dosyasını verme")
                await page.locator(SELECTORS["file_input"]).first.set_input_files(str(video_path))
                logger.info("Video dosyası verildi: %s", video_path.name)
                await self._handle_identity_check(page)

                step = "başlık/açıklama girme"
                await self._fill_details(page, title, description, tags)
                await self._handle_identity_check(page)

                step = "thumbnail yükleme"
                await self._set_thumbnail(page, thumbnail_path, on_progress)
                await self._handle_identity_check(page)

                step = "kitle (çocuk) seçimi"
                await self._set_not_for_kids(page)

                if on_screenshot:
                    await on_screenshot(await page.screenshot(full_page=False),
                                        "📝 Video ayrıntıları girildi.")

                step = "adımlar arası ilerleme"
                await self._advance_steps(page, on_progress)

                step = "yükleme/kontrollerin tamamlanmasını bekleme"
                await self._wait_processing(page, on_progress)

                step = "görünürlük seçimi (Herkese Açık)"
                await page.locator(SELECTORS["public_radio"]).first.click()
                await _human_pause()

                step = "video linkini okuma"
                video_url = await self._read_video_link(page)

                step = "yayınlama"
                await self._assert_channel(page, channel_name, "yayınlama")
                await page.locator(SELECTORS["done_button"]).first.click()
                video_url = await self._confirm_published(page, video_url)

                if on_screenshot:
                    await on_screenshot(await page.screenshot(full_page=False), "🎉 Yayınlandı!")

                logger.info("Yükleme tamamlandı: %s", video_url)
                return video_url
            except (SessionExpiredError, StudioUploadError):
                raise
            except Exception as exc:  # noqa: BLE001 — adım bilgisiyle sarmala
                screenshot = await self._safe_screenshot(page)
                logger.exception("Studio yüklemesi '%s' adımında başarısız.", step)
                raise StudioUploadError(step, f"'{step}' adımında hata: {exc}", screenshot) from exc
            finally:
                await context.close()

    # ------------------------------------------------------------ adımlar

    async def _check_session(self, page: Page) -> None:
        """Studio'yu açar; login sayfasına yönlendirilirse oturum düşmüş demektir."""
        await page.goto(STUDIO_URL, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except PlaywrightTimeoutError:
            pass  # networkidle gelmese de URL kontrolü yeterli
        if "accounts.google.com" in page.url or "/signin" in page.url:
            raise SessionExpiredError(
                "Google oturumu düşmüş. /giris komutuyla yeniden giriş yapın."
            )
        await self._dismiss_browser_warning(page)
        # Check identity verification during startup session check too
        is_verify = await self._handle_identity_check(page)
        if not is_verify and any(t in page.url for t in ["accounts.google.com", "signin"]):
            raise SessionExpiredError(
                "Google 'Kimliğinizi doğrulayın' diyaloğu çıktı ve onaylanmadı veya oturum düşmüş."
            )

    async def _identity_dialog_visible(self, page: Page) -> bool:
        for text in TEXTS["verify_identity"]:
            try:
                if await page.get_by_text(text, exact=False).first.is_visible():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _manual_identity_check(self, page: Page) -> bool:
        """Ekranı yayınlar ve kullanıcı doğrulamayı bu tarayıcıda bitirene kadar bekler.

        Raises:
            StudioUploadError: Süre içinde doğrulama tamamlanmazsa.
        """
        context = page.context
        pages_before = len(context.pages)
        chat_id = getattr(self, "_chat_id", None)

        async def verification_done() -> bool:
            if len(context.pages) > pages_before:  # Google'ın doğrulama sekmesi hâlâ açık
                return False
            if _is_login_url(page.url):
                return False
            return not await self._identity_dialog_visible(page)

        async with vnc.Broadcast() as broadcast:
            if chat_id is not None:
                vnc.TYPE_TARGETS[chat_id] = lambda: context.pages[-1] if context.pages else None
            try:
                message = (
                    "🔑 <b>Google kimlik doğrulaması istiyor</b> — yükleme sizi bekliyor.\n\n"
                    f"1. Şu adresi açıp <b>Connect</b>'e basın:\n{vnc.url()}\n"
                    f"2. {broadcast.password_line}\n"
                    "3. Ekrandaki tarayıcıda <b>Sonraki</b>'ye basıp doğrulamayı tamamlayın "
                    "(parola, telefon onayı ya da kod).\n\n"
                    "Bitince başka bir şey yapmanız gerekmez; yükleme kendiliğinden devam eder.\n"
                    "⌨️ Uzak ekranda yazamadığınız metni bu sohbete yazın, tarayıcıya yazılır.\n"
                    f"Süre: {MANUAL_VERIFY_MINUTES} dakika."
                )
                screenshot = await self._safe_screenshot(page)
                if screenshot and self._on_screenshot:
                    await self._on_screenshot(screenshot, message)
                elif self._on_progress:
                    await self._on_progress(message)

                deadline = asyncio.get_running_loop().time() + MANUAL_VERIFY_MINUTES * 60
                calm = 0  # art arda kaç denetimde doğrulama bitmiş göründü
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(2)
                    try:
                        calm = calm + 1 if await verification_done() else 0
                    except Exception:  # noqa: BLE001 — sayfa geçiş halindeyken okunamayabilir
                        calm = 0
                    if calm >= 3:
                        logger.info("Kimlik doğrulaması kullanıcı tarafından tamamlandı.")
                        if self._on_progress:
                            await self._on_progress("✅ Kimlik doğrulandı, yükleme devam ediyor...")
                        return True
            finally:
                if chat_id is not None:
                    vnc.TYPE_TARGETS.pop(chat_id, None)

        raise StudioUploadError(
            "kimlik doğrulama",
            f"Google kimlik doğrulaması {MANUAL_VERIFY_MINUTES} dakika içinde tamamlanmadı. "
            "🔁 Tekrar dene'ye basınca ekran yeniden açılır.",
            await self._safe_screenshot(page),
        )

    async def _handle_identity_check(self, page: Page) -> bool:
        """Google'ın 'Kimliğinizi doğrulayın' diyaloğu çıktığında bota bildirim
        gönderip kurtarma e-postası seçeneğini tetikler ve kullanıcının gireceği kodu bekler.
        """
        if not await self._identity_dialog_visible(page):
            return False

        logger.info("Kimliğinizi doğrulayın diyaloğu tespit edildi! İşlem duraklatılıyor...")
        # Sunucuda: ekran kullanıcıya açılır, doğrulamayı AYNI tarayıcıda kendisi geçer.
        # (Doğrulama /giris ile ayrı bir tarayıcı oturumunda geçilince yüklemeye taşınmıyor.)
        if vnc.unavailable_reason() is None:
            return await self._manual_identity_check(page)

        if self._on_progress:
            await self._on_progress("🔑 Google kimlik doğrulaması istedi. 'Sonraki' butonuna basılıyor...")

        pages_before = len(page.context.pages)
        next_button = None
        dialogs = page.locator("ytcp-dialog, tp-yt-paper-dialog, [role=dialog], [role=alertdialog]")
        for verify_text in TEXTS["verify_identity"]:
            scoped = dialogs.filter(has_text=verify_text)
            for btn_text in ["Sonraki", "Next"]:
                btn = scoped.get_by_role("button", name=btn_text).last
                try:
                    if await btn.is_visible():
                        next_button = btn
                        break
                except Exception:
                    continue
            if next_button:
                break
        if not next_button:
            for btn_text in ["Sonraki", "Next"]:
                btn = page.get_by_role("button", name=btn_text).last
                try:
                    if await btn.is_visible():
                        next_button = btn
                        break
                except Exception:
                    continue

        if not next_button:
            next_button = page.locator("ytcp-button[id=confirm-button], paper-button[id=confirm-button]").first

        try:
            await next_button.click(timeout=5000)
            logger.info("Doğrulama Sonraki butonuna tıklandı.")
        except Exception as exc:
            logger.warning("Sonraki butonuna tıklanamadı: %s", exc)

        # Onay seçenekleri ekranının yüklenmesi için bekle
        await asyncio.sleep(4)

        # Google doğrulamayı yeni bir sekmede/pencerede açabiliyor; öyleyse oradan devam edilir.
        origin = page
        if len(page.context.pages) > pages_before:
            page = page.context.pages[-1]
            logger.info("Doğrulama yeni sekmede açıldı: %s", page.url.split("?")[0])
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:  # noqa: BLE001
                pass

        # 1. "Başka bir yöntem dene" (Try another way) linkini/butonunu bul ve tıkla
        try:
            try_another = None
            for text in ["Başka bir yöntem dene", "Try another way", "Try another way to sign in"]:
                btn = page.get_by_text(text, exact=False).first
                if await btn.is_visible():
                    try_another = btn
                    break
            
            if try_another:
                await try_another.click()
                logger.info("Başka bir yöntem dene tıklandı.")
                await asyncio.sleep(3)
        except Exception as exc:
            logger.warning("Başka bir yöntem dene tıklanamadı: %s", exc)

        # 2. Kurtarma e-postası (Recovery email) seçeneğini bul ve tıkla
        try:
            email_option = None
            texts_to_check = [
                "kurtarma e-postanıza", "kurtarma", "recovery", "kurtarma e-posta", 
                "recovery email", "kurtarma e-postası", "kod gönder"
            ]
            for opt_text in texts_to_check:
                btn = page.get_by_text(opt_text, exact=False).first
                if await btn.is_visible():
                    email_option = btn
                    break
            
            if not email_option:
                items = await page.locator("li, div[role=button]").all()
                for item in items:
                    txt = await item.inner_text()
                    if "@" in txt or "kurtarma" in txt.lower() or "recovery" in txt.lower():
                        email_option = item
                        break

            if email_option:
                await email_option.click()
                logger.info("Kurtarma e-postası seçeneği tıklandı.")
                await asyncio.sleep(4)
        except Exception as exc:
            logger.warning("Kurtarma e-postası seçilemedi: %s", exc)

        # 3. Kod kutusu gerçekten açıldı mı? Açılmadıysa otomatik yol tıkanmıştır:
        #    "kod gönderildi" demek yerine kullanıcı uzak ekrana yönlendirilir.
        code_input = page.locator(_CODE_INPUT).first
        try:
            # Kod yalnızca Google'ın kendi doğrulama sayfasında girilir; hâlâ Studio'daysak
            # ("Sonraki" tutmadı) oradaki başka bir metin kutusu kod kutusu sanılmamalı.
            if "accounts.google.com" not in page.url:
                raise PlaywrightTimeoutError("Google doğrulama sayfası açılmadı.")
            await code_input.wait_for(state="visible", timeout=8_000)
        except PlaywrightTimeoutError as exc:
            raise StudioUploadError(
                "kimlik doğrulama",
                "Google 'Kimliğinizi doğrulayın' diyor ve bu adım otomatik geçilemedi. "
                "/giris yazıp uzak ekranı açın; orada Oluştur → Video yükle deyip çıkan "
                "doğrulamayı kendiniz tamamlayın, sonra 🔁 Tekrar dene'ye basın.",
                await self._safe_screenshot(page),
            ) from exc

        screenshot = await self._safe_screenshot(page)
        if screenshot and self._on_screenshot:
            await self._on_screenshot(
                screenshot,
                "📬 <b>Google bir doğrulama kodu istiyor.</b>\n\n"
                "Ekran görüntüsünde yazan yere (kurtarma e-postası / telefon) gelen "
                "<b>doğrulama kodunu</b> bota doğrudan mesaj olarak gönderin.\n"
                "Zaman aşımı süresi: 3 dakika."
            )

        # 4. Kullanıcının kodu bota girmesini bekle
        future = asyncio.Future()
        VERIFICATION_FUTURES[self._chat_id] = future
        
        try:
            # Wait up to 3 minutes for code entry
            code = await asyncio.wait_for(future, timeout=180.0)
            logger.info("Kullanıcıdan gelen kod: %s", code)
        except asyncio.TimeoutError:
            logger.warning("Doğrulama kodu zaman aşımı.")
            if self._on_progress:
                await self._on_progress("❌ Doğrulama kodu zamanında girilmedi. Yükleme iptal ediliyor.")
            return False
        finally:
            VERIFICATION_FUTURES.pop(self._chat_id, None)

        # 5. Kodu tarayıcıya gir
        try:
            await code_input.fill(code)
            await _human_pause(0.5, 1.0)
            
            next_btn = None
            for txt in ["Sonraki", "Next"]:
                btn = page.get_by_role("button", name=txt).first
                if await btn.is_visible():
                    next_btn = btn
                    break
            if not next_btn:
                next_btn = page.locator("button[type=submit], #next, #submit").first
                
            await next_btn.click()
            logger.info("Doğrulama kodu gönderildi, yönlendirme bekleniyor...")
            await asyncio.sleep(5)
        except Exception as exc:
            logger.warning("Kod girilirken hata: %s", exc)
            return False

        # Son kontrol: YouTube Studio'ya geri yönlendirildik mi?
        if origin is not page and not page.is_closed():
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
        page = origin
        if "studio.youtube.com" in page.url and "accounts.google.com" not in page.url:
            logger.info("Doğrulama başarılı, devam ediliyor!")
            if self._on_progress:
                await self._on_progress("✅ Google kimlik doğrulaması başarıyla onaylandı! Yükleme devam ediyor...")
            await asyncio.sleep(3)
            return True
        else:
            logger.warning("Doğrulama başarısız oldu (Studio'ya dönülemedi).")
            return False

    async def _dismiss_browser_warning(self, page: Page) -> None:
        """Studio'nun 'desteklenmeyen tarayıcı' ara sayfasını (çıkarsa) geçer."""
        for text in TEXTS["continue_anyway"]:
            link = page.get_by_text(text, exact=False).first
            try:
                await link.click(timeout=2_500)
                logger.info("Tarayıcı uyarısı sayfası geçildi ('%s').", text)
                await page.wait_for_load_state("domcontentloaded")
                await _human_pause()
                return
            except PlaywrightTimeoutError:
                continue

    async def _active_channel(self, page: Page, timeout_ms: int = 30_000) -> str | None:
        """Studio'nun sol menüsünde yazan aktif kanal adını okur (okuyamazsa None).

        Yükleme diyaloğu açıkken de çalışır: öğe görünür olmasa da DOM'dadır.
        """
        loc = page.locator(SELECTORS["active_channel"]).first
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while True:
            try:
                text = (await loc.text_content(timeout=2_000) or "").strip()
            except Exception:  # noqa: BLE001 — öğe henüz yok
                text = ""
            if text:
                return text
            if asyncio.get_running_loop().time() >= deadline:
                logger.warning("Aktif kanal adı okunamadı.")
                return None
            await asyncio.sleep(0.5)

    async def _ensure_channel(self, context: BrowserContext, page: Page, channel_name: str) -> None:
        """Hedef kanalın Studio'sunu kanal kimliğiyle açar ve doğru kanalda olduğunu doğrular.

        youtube.com/channel_switcher üzerinden kanal değiştirmek güvenilir değil:
        tıklama kabul edilse de Studio aynı oturumda eski kanalı göstermeye devam
        ediyor. Studio'yu ``/channel/<kimlik>`` adresiyle açmak ise kanalı anında
        ve kesin olarak seçiyor. Kanal doğrulanamazsa yükleme BAŞLAMAZ.
        """
        wanted = _norm(channel_name)
        # Studio kök adresten /channel/<kimlik>'e kendi yönlendirmesini bitirsin;
        # yoksa aşağıdaki geçişi geri alabiliyor.
        try:
            await page.wait_for_url("**/channel/**", timeout=15_000)
        except PlaywrightTimeoutError:
            pass

        ucid = db.get_channel_ucid(channel_name)
        if not ucid:
            ucid = await self._discover_ucid(page, channel_name)
            db.set_channel_ucid(channel_name, ucid)
        self._ucid = ucid

        active: str | None = None
        for _ in range(3):
            await page.goto(_studio_channel_url(ucid), wait_until="domcontentloaded")
            await self._dismiss_browser_warning(page)
            await asyncio.sleep(1.0)
            if f"/channel/{ucid}" not in page.url:
                continue  # başka bir yönlendirme araya girdiyse yeniden dene
            active = await self._active_channel(page)
            if _norm(active or "") == wanted:
                logger.info("Kanal doğrulandı: %s (%s)", channel_name, ucid)
                await _human_pause()
                return

        raise StudioUploadError(
            "kanal doğrulama",
            f"Studio '{active or 'okunamadı'}' kanalını gösteriyor; beklenen '{channel_name}'. "
            "Yükleme başlatılmadı. /kanallar → 'Kanalları otomatik bul' ile kanal kaydını yenileyin.",
            await self._safe_screenshot(page),
        )

    async def _assert_channel(self, page: Page, channel_name: str, step: str) -> None:
        """Kritik adımlardan hemen önce hâlâ doğru kanalda olduğumuzu denetler."""
        active = await self._active_channel(page, timeout_ms=10_000)
        if active is not None:
            ok = _norm(active) == _norm(channel_name)
        else:
            ucid = getattr(self, "_ucid", None)
            ok = bool(ucid) and f"/channel/{ucid}" in page.url
        if not ok:
            raise StudioUploadError(
                "kanal doğrulama",
                f"'{step}' öncesinde kanal doğrulanamadı: Studio '{active or 'okunamadı'}' "
                f"gösteriyor, beklenen '{channel_name}'. İşlem durduruldu.",
                await self._safe_screenshot(page),
            )

    async def _discover_ucid(self, page: Page, channel_name: str) -> str:
        """Kanal kimliği kayıtlı değilse hesaptaki kanallardan ada göre bulur."""
        entries = await _read_switcher(page)
        wanted = _norm(channel_name)
        entry = next((e for e in entries if _norm(e["name"]) == wanted), None)
        if entry is None:
            names = ", ".join(e["name"] for e in entries) or "—"
            raise StudioUploadError(
                "kanal bulma",
                f"'{channel_name}' adlı kanal bulunamadı. Hesaptaki kanallar: {names}. "
                "/kanallar → 'Kanalları otomatik bul' ile kanalları yeniden kaydedin.",
                await self._safe_screenshot(page),
            )
        ucid = await _resolve_handle(page, entry["handle"]) if entry["handle"] else None
        if not ucid:
            raise StudioUploadError(
                "kanal bulma",
                f"'{channel_name}' kanalının kimliği okunamadı.",
                await self._safe_screenshot(page),
            )
        return ucid

    async def is_session_alive(self) -> bool:
        """Studio'yu kalıcı profille açıp Google oturumunun sürdüğünü denetler.

        Günlük kontrol için: oturum çerezlerini taze tutar ve oturum düşmüşse
        bunu bir yükleme sırasında değil, önceden fark etmeyi sağlar.
        """
        _clear_profile_locks(self._cfg)
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(**_launch_kwargs(self._cfg))
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                page.set_default_timeout(60_000)
                await page.goto(STUDIO_URL, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=20_000)
                except PlaywrightTimeoutError:
                    pass  # networkidle gelmese de adres kontrolü yeterli
                if _is_login_url(page.url):
                    return False
                return "studio.youtube.com" in page.url
            finally:
                await context.close()

    async def detect_channels(self) -> list[dict[str, str]]:
        """Hesaptaki kanalları bulur: ad + kanal kimliği, Studio'da doğrulanmış olarak.

        Returns:
            ``{"name", "ucid"}`` sözlükleri (yalnızca Studio'da adı doğrulananlar).

        Raises:
            SessionExpiredError: Google oturumu düşmüşse.
        """
        _clear_profile_locks(self._cfg)
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(**_launch_kwargs(self._cfg))
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(60_000)
            try:
                found: list[dict[str, str]] = []
                for entry in await _read_switcher(page):
                    if not entry["handle"]:
                        continue
                    ucid = await _resolve_handle(page, entry["handle"])
                    if not ucid:
                        continue
                    await page.goto(_studio_channel_url(ucid), wait_until="domcontentloaded")
                    await self._dismiss_browser_warning(page)
                    active = await self._active_channel(page)
                    if _norm(active or "") == _norm(entry["name"]):
                        found.append({"name": entry["name"], "ucid": ucid})
                    else:
                        logger.warning("Kanal doğrulanamadı: %r → Studio %r gösterdi.",
                                       entry["name"], active)
                return found
            finally:
                await context.close()

    async def _open_upload_dialog(self, page: Page) -> None:
        """Oluştur → Video yükle (buton rol+isimle, menü metni TR/EN denenir)."""
        for name in TEXTS["create"]:
            button = page.get_by_role("button", name=name).first
            try:
                await button.click(timeout=5_000)
                break
            except PlaywrightTimeoutError:
                continue
        else:
            # Rol tabanlı bulunamazsa eski kimlikli seçiciler denenir.
            await page.locator(SELECTORS["create_button"]).first.click()
        await _human_pause(0.3, 0.8)
        for text in TEXTS["upload_videos"]:
            item = page.get_by_text(text, exact=False).first
            try:
                await item.click(timeout=3_000)
                break
            except PlaywrightTimeoutError:
                continue
        else:
            # Metin bulunamadıysa menüdeki ilk öğe (Upload videos) denenir.
            await page.locator(SELECTORS["upload_menu_item"]).first.click(timeout=10_000)
        # ytcp-uploads-dialog ekranda görünse bile Playwright'a "hidden" raporlanabiliyor;
        # dosya gizli input'a verildiği için DOM'a eklenmiş olmaları yeterli.
        await page.locator(SELECTORS["upload_dialog"]).first.wait_for(state="attached", timeout=30_000)
        await page.locator(SELECTORS["file_input"]).first.wait_for(state="attached", timeout=30_000)

    async def _fill_textbox(self, page: Page, selector: str, value: str, field_name: str) -> None:
        """Contenteditable alanı temizleyip yazar; sonra içeriği doğrular."""
        box = page.locator(selector).first
        await box.wait_for(state="visible", timeout=120_000)  # işleme başlangıcı gecikebilir
        await box.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        await _human_pause(0.3, 0.8)
        await box.fill(value)
        await _human_pause()
        # Otomatik taslak/kayıt gecikmelerine karşı doğrulama + tek tekrar.
        actual = _norm(await box.inner_text())
        if actual != _norm(value):
            logger.warning("%s alanı doğrulanamadı, yeniden yazılıyor.", field_name)
            await box.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            await box.press_sequentially(value, delay=15)
            await _human_pause()
            actual = _norm(await box.inner_text())
            if actual != _norm(value):
                raise StudioUploadError(
                    "başlık/açıklama girme",
                    f"{field_name} alanına metin yazılamadı (alan içeriği beklenenle eşleşmiyor).",
                    await self._safe_screenshot(page),
                )

    async def _fill_details(self, page: Page, title: str, description: str, tags: list[str] | None = None) -> None:
        await self._fill_textbox(page, SELECTORS["title_box"], title, "Başlık")
        if description:
            await self._fill_textbox(page, SELECTORS["description_box"], description, "Açıklama")
        if tags:
            await self._set_tags(page, tags)

    async def _set_tags(self, page: Page, tags: list[str]) -> None:
        """Video etiketlerini YouTube Studio'ya girer."""
        try:
            show_more_text = ["Daha fazla göster", "Show more", "SHOW MORE"]
            for text in show_more_text:
                btn = page.get_by_text(text, exact=False).first
                try:
                    await btn.click(timeout=3_000)
                    logger.info("Daha fazla göster tıklandı.")
                    await _human_pause(0.5, 1.0)
                    break
                except PlaywrightTimeoutError:
                    continue
            else:
                await page.locator("#toggle-button").first.click(timeout=3_000)
                await _human_pause(0.5, 1.0)

            tags_str = ", ".join(tags)
            tags_input = page.locator("#tags-container #text-input, #tags-container input[type=text]").first
            await tags_input.wait_for(state="visible", timeout=5_000)
            await tags_input.click()
            await tags_input.fill(tags_str)
            await tags_input.press("Enter")
            await _human_pause(0.5, 1.0)
            logger.info("Etiketler girildi: %s", tags_str)
        except Exception as exc:
            logger.warning("Etiketler girilirken hata oluştu: %s", exc)

    async def _set_thumbnail(self, page: Page, thumbnail_path: Path | None,
                             on_progress: ProgressCallback | None) -> None:
        """Sabit thumbnail'i verir; kanal telefon doğrulamasızsa adımı atlar."""
        if thumbnail_path is None or not thumbnail_path.exists():
            logger.warning("Thumbnail dosyası yok, adım atlanıyor: %s", thumbnail_path)
            if on_progress:
                await on_progress("ℹ️ Thumbnail dosyası bulunamadı, bu adım atlandı.")
            return
        thumb_input = page.locator(SELECTORS["thumbnail_input"]).first
        try:
            await thumb_input.wait_for(state="attached", timeout=10_000)
            await thumb_input.set_input_files(str(thumbnail_path))
            await _human_pause()
            logger.info("Thumbnail verildi: %s", thumbnail_path.name)
        except PlaywrightTimeoutError:
            logger.warning("Thumbnail input'u bulunamadı (kanal telefon doğrulaması yapılmamış olabilir).")
            if on_progress:
                await on_progress(
                    "ℹ️ Özel thumbnail yüklenemedi — kanalın telefon doğrulaması yapılmamış "
                    "olabilir. Video thumbnail'siz devam ediyor."
                )

    async def _set_not_for_kids(self, page: Page) -> None:
        """'Hayır, çocuklara özel değil' seçilir; yaş kısıtlaması EKLENMEZ."""
        radio = page.locator(SELECTORS["kids_no_radio"]).first
        await radio.wait_for(state="visible", timeout=30_000)
        await radio.click()
        await _human_pause()

    async def _advance_steps(self, page: Page, on_progress: ProgressCallback | None) -> None:
        """'İleri' ×3: Video öğeleri ve Kontroller varsayılan bırakılır."""
        for i in range(3):
            next_btn = page.locator(SELECTORS["next_button"]).first
            await next_btn.wait_for(state="visible", timeout=30_000)
            await next_btn.click()
            await _human_pause()
            if on_progress and i == 2:
                await on_progress("🔎 Telif kontrollerinin tamamlanması bekleniyor...")

    async def _read_progress_label(self, page: Page) -> str:
        try:
            label = page.locator(SELECTORS["progress_label"]).first
            if await label.count():
                return _norm(await label.inner_text())
        except Exception:  # noqa: BLE001 — etiket geçici olarak kaybolabilir
            pass
        return ""

    async def _wait_processing(self, page: Page, on_progress: ProgressCallback | None) -> None:
        """Yükleme yüzdesini raporlar; kontroller bitene dek akıllı bekleme yapar.

        Kapatmadan önce dosya yüklemesinin bitmesi ŞARTTIR (aksi halde yükleme
        yarıda kalır). Kontroller makul sürede bitmezse — kontroller yayından
        sonra da devam edebildiği için — yalnızca yükleme bitmişse ilerlenir.
        """
        deadline = asyncio.get_event_loop().time() + self._cfg.upload_timeout_minutes * 60
        last_reported = ""
        upload_finished = False
        while asyncio.get_event_loop().time() < deadline:
            text = await self._read_progress_label(page)
            if text and text != last_reported and on_progress:
                await on_progress(f"⏳ Studio: {text}")
                last_reported = text

            still_uploading = any(t in text for t in TEXTS["uploading"]) and "%" in text
            if not still_uploading and text:
                upload_finished = True
            if any(t in text for t in TEXTS["checks_done"]):
                logger.info("Kontroller tamamlandı: %r", text)
                return
            # Kontroller uzarsa: yükleme bitti + 90 sn geçtiyse devam et.
            if upload_finished and any(t in text for t in TEXTS["upload_done"]):
                logger.info("Yükleme bitti (%r); kontroller arka planda sürebilir, devam ediliyor.", text)
                return
            await asyncio.sleep(5)

        if not upload_finished:
            raise StudioUploadError(
                "yükleme/kontrollerin tamamlanmasını bekleme",
                f"Yükleme {self._cfg.upload_timeout_minutes} dakika içinde tamamlanmadı.",
                await self._safe_screenshot(page),
            )

    async def _read_video_link(self, page: Page) -> str | None:
        """Ayrıntılar panelindeki video linkini okur (yayın öncesi yedek)."""
        try:
            anchor = page.locator(SELECTORS["video_link_anchor"]).first
            href = await anchor.get_attribute("href", timeout=10_000)
            return href
        except PlaywrightTimeoutError:
            return None

    async def _confirm_published(self, page: Page, fallback_url: str | None) -> str:
        """Yayınlandı diyaloğundan video linkini alır.

        Telif kontrolleri sürerken yayınlanırsa Studio "İçeriğinizi kontrol
        etmeye devam ediyoruz" onayı çıkarır — "Yine de yayınla" ile geçilir
        (kontroller yayından sonra da devam eder).
        """
        for text in TEXTS["publish_anyway"]:
            button = page.get_by_role("button", name=text)
            try:
                await button.first.click(timeout=5_000)
                logger.info("Kontroller sürüyor uyarısı geçildi ('%s').", text)
                await _human_pause()
                break
            except PlaywrightTimeoutError:
                continue
        try:
            await page.locator(SELECTORS["share_dialog"]).first.wait_for(state="visible", timeout=60_000)
            share = page.locator(SELECTORS["share_url"]).first
            href = await share.get_attribute("href", timeout=10_000)
            if not href:
                href = (await share.inner_text()).strip()
            if href:
                return href
        except PlaywrightTimeoutError:
            logger.warning("Yayınlandı diyaloğu görülemedi; yedek link kullanılacak.")
        if fallback_url:
            return fallback_url
        raise StudioUploadError(
            "yayınlama",
            "Video yayınlandı görünüyor ancak linki okunamadı. Studio'daki İçerik "
            "sayfasından kontrol edin.",
            await self._safe_screenshot(page),
        )

    async def _safe_screenshot(self, page: Page) -> bytes | None:
        try:
            return await page.screenshot(full_page=True)
        except Exception:  # noqa: BLE001
            return None


async def login_setup(config: Config | None = None) -> None:
    """Kurulum modu: headful Chrome açar, kullanıcı elle Google'a girer.

    Kullanıcı studio.youtube.com'a giriş yapıp terminalde Enter'a basınca
    profil kaydedilir ve tarayıcı kapanır.
    """
    cfg = config or get_config()
    print("\n=== Google oturum kurulumu ===")
    print("Açılan Chrome penceresinde Google hesabınıza giriş yapın ve")
    print("studio.youtube.com'un açıldığını görün. İki kanal arasında geçişi de deneyin.")
    print("Bittiğinde bu terminale dönüp Enter'a basın.\n")
    for lock_name in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        lock_file = cfg.browser_profile_dir / lock_name
        if lock_file.exists() or lock_file.is_symlink():
            try:
                lock_file.unlink()
            except Exception:
                pass
                
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(cfg.browser_profile_dir),
            headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(STUDIO_URL, wait_until="domcontentloaded")
        await asyncio.to_thread(input, "Giriş tamamlandıysa Enter'a basın... ")
        await context.close()
    print("✅ Oturum profili kaydedildi:", cfg.browser_profile_dir)
    print("Artık botu normal modda başlatabilirsiniz: python -m bot")
