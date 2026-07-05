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
from typing import Awaitable, Callable

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ..config import Config, get_config

logger = logging.getLogger(__name__)

# Aynı anda tek yükleme: ikinci /baslat akışı bu kilit serbest kalmadan
# Studio'ya dokunamaz.
UPLOAD_LOCK = asyncio.Lock()

STUDIO_URL = "https://studio.youtube.com/"
CHANNEL_SWITCHER_URL = "https://www.youtube.com/channel_switcher"

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


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


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
        thumbnail_path: Path | None = None,
        on_progress: ProgressCallback | None = None,
        on_screenshot: ScreenshotCallback | None = None,
    ) -> str:
        """Videoyu seçilen kanala yükler, Herkese Açık yayınlar; video linkini döndürür.

        Raises:
            SessionExpiredError: Google oturumu düşmüşse.
            StudioUploadError: Herhangi bir adım başarısız olursa (ekran
                görüntüsüyle birlikte).
        """
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

                step = "kanal değiştirme"
                await self._ensure_channel(context, page, channel_name)

                step = "yükleme diyaloğunu açma"
                await self._open_upload_dialog(page)

                step = "video dosyasını verme"
                await page.locator(SELECTORS["file_input"]).first.set_input_files(str(video_path))
                logger.info("Video dosyası verildi: %s", video_path.name)

                step = "başlık/açıklama girme"
                await self._fill_details(page, title, description)

                step = "thumbnail yükleme"
                await self._set_thumbnail(page, thumbnail_path, on_progress)

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
                "Google oturumu düşmüş. Bilgisayarda 'python -m bot --login-setup' "
                "çalıştırıp yeniden giriş yapın."
            )
        await self._dismiss_browser_warning(page)
        await self._fail_if_identity_check(page)

    async def _fail_if_identity_check(self, page: Page) -> None:
        """Google'ın 'Kimliğinizi doğrulayın' diyaloğu tüm tıklamaları engeller;
        elle doğrulama gerektiği için anlaşılır bir hatayla durulur."""
        for text in TEXTS["verify_identity"]:
            modal = page.get_by_text(text, exact=False).first
            try:
                if await modal.is_visible(timeout=1_500):
                    raise SessionExpiredError(
                        "Google 'Kimliğinizi doğrulayın' diyaloğu çıkardı. Bilgisayarda "
                        "'python -m bot --login-setup' çalıştırıp açılan pencerede "
                        "doğrulamayı tamamlayın, sonra tekrar deneyin."
                    )
            except PlaywrightTimeoutError:
                continue

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

    async def _current_channel_name(self, page: Page) -> str | None:
        """Studio avatar menüsünden aktif kanal adını okur (okuyamazsa None)."""
        try:
            await page.locator(SELECTORS["account_button"]).first.click(timeout=15_000)
            name_loc = page.locator(SELECTORS["account_name"]).first
            await name_loc.wait_for(state="visible", timeout=10_000)
            name = _norm(await name_loc.inner_text())
            await page.keyboard.press("Escape")
            await _human_pause(0.3, 0.7)
            return name
        except PlaywrightTimeoutError:
            logger.warning("Aktif kanal adı okunamadı; kanal değiştirici kullanılacak.")
            try:
                await page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            return None

    async def _ensure_channel(self, context: BrowserContext, page: Page, channel_name: str) -> None:
        """Doğru kanalda değilsek youtube.com/channel_switcher ile kanala geçer."""
        current = await self._current_channel_name(page)
        wanted = _norm(channel_name)
        if current == wanted:
            logger.info("Zaten doğru kanaldayız: %s", channel_name)
            return

        logger.info("Kanal değiştiriliyor: %r → %r", current, channel_name)
        await page.goto(CHANNEL_SWITCHER_URL, wait_until="domcontentloaded")
        items = page.locator(SELECTORS["switcher_item"])
        try:
            await items.first.wait_for(state="visible", timeout=30_000)
        except PlaywrightTimeoutError as exc:
            raise StudioUploadError(
                "kanal değiştirme",
                "Kanal seçim sayfası (youtube.com/channel_switcher) yüklenemedi.",
                await self._safe_screenshot(page),
            ) from exc

        count = await items.count()
        names: list[str] = []
        for i in range(count):
            item = items.nth(i)
            raw = await item.locator(SELECTORS["switcher_item_name"]).first.inner_text()
            names.append(raw.strip())
            if _norm(raw) == wanted:
                await item.click()
                await page.wait_for_load_state("domcontentloaded")
                await _human_pause(1.0, 2.0)
                await page.goto(STUDIO_URL, wait_until="domcontentloaded")
                await self._dismiss_browser_warning(page)
                await _human_pause()
                return

        raise StudioUploadError(
            "kanal değiştirme",
            f"'{channel_name}' adlı kanal bulunamadı. Hesaptaki kanallar: {', '.join(names) or '—'}. "
            "/kanallar komutuyla kayıtlı adın Studio'daki görünen adla birebir aynı olduğundan emin olun.",
            await self._safe_screenshot(page),
        )

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

    async def _fill_details(self, page: Page, title: str, description: str) -> None:
        await self._fill_textbox(page, SELECTORS["title_box"], title, "Başlık")
        if description:
            await self._fill_textbox(page, SELECTORS["description_box"], description, "Açıklama")

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
        """Yayınlandı diyaloğundan video linkini alır."""
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
