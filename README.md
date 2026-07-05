# Telegram Onaylı Drive → YouTube Yükleme Botu

Google Drive'daki şarkı klasörlerini (audio + Word açıklama) Telegram'dan onay alarak
1080p videoya çevirir ve **YouTube Studio arayüzü üzerinden (Playwright)** Herkese Açık
olarak yayınlar. Tek kullanıcılı, kişisel bir otomasyon botudur; kendi bilgisayarınızda
lokal çalışır.

> **Neden API değil de Playwright?** YouTube Data API ile doğrulanmamış projelerden
> yüklenen videolar YouTube tarafından **private kilitleniyor** ve compliance audit
> gerekiyor. Studio arayüzünden yükleme normal yükleme sayılır — kilit yok, kota yok,
> audit yok. Drive erişimi ise API ile kalır (salt okuma; orada böyle bir sorun yoktur).

## Özellikler

- `/baslat` — Drive ana klasörünü tarar, işlenmemiş yeni alt klasörleri bulur
- Adım adım onay akışı: klasör → kanal → görsel → başlık → açıklama → son onay → yükle
- Görsel + audio → ffmpeg ile 1920×1080 H.264/AAC mp4 (letterbox, oran korunur)
- Playwright ile Studio yüklemesi: başlık/açıklama, sabit thumbnail, "çocuklara özel
  değil", telif kontrolü beklemesi, **Herkese Açık** yayın, video linki
- Kritik anlarda Telegram'a ekran görüntüsü (ayrıntılar girildi / yayınlandı / hata anı)
- Hata durumunda mp4 saklanır; **🔁 Tekrar dene** ile video yeniden üretilmeden yalnızca
  yükleme adımı tekrarlanır
- `/istatistik` — haftalık/aylık/toplam rapor, kanal kırılımı, son 10 yükleme
- `/gorseller` — görsel kütüphanesi (listele/sil); akış içinde telefondan görsel ekleme
- `/kanallar` — Studio'daki görünen kanal adlarının kaydı (kanal değiştirme buna göre yapılır)
- Whitelist güvenliği: izinsiz kullanıcıya **hiç cevap verilmez**; deneme olursa size
  bildirim gelir (aynı ID için saatte en fazla 1)

---

## Hızlı Kurulum (Windows — önerilen)

```bat
git clone https://github.com/Piktes/basaksoto.git
cd basaksoto
kurulum.bat
```

`kurulum.bat` her şeyi kurar (pip paketleri, Chromium, ffmpeg) ve `.env` şablonunu oluşturur.
Sonra sırasıyla:

1. `.env` dosyasını düzenleyin (bot token + Telegram ID'ler)
2. `credentials\client_secret.json` dosyasını koyun (aşağıda adım 3)
3. `google_giris.bat` → Google/YouTube oturumunu bir kez açın
4. `bot_baslat.bat` → bot çalışır

> Yeni bilgisayarda Google ilk girişte ek doğrulama isteyebilir (telefon onayı) — normaldir,
> bir kez yapılır ve profile kaydedilir.

## Elle Kurulum (adım adım)

### 1. Python ve ffmpeg

- **Python 3.11+** kurulu olmalı (`python --version`).
- **ffmpeg**:
  - Windows: `winget install ffmpeg` → kurulumdan sonra **terminali yeniden açın** ve
    `ffmpeg -version` ile PATH'te olduğunu doğrulayın.
  - Linux: `sudo apt install ffmpeg`

### 2. Bağımlılıklar

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Google Cloud (yalnızca Drive API)

1. [Google Cloud Console](https://console.cloud.google.com/)'da yeni proje oluşturun.
2. **APIs & Services → Library** → yalnızca **Google Drive API**'yi etkinleştirin
   (YouTube API'ye dokunmayın — kullanılmıyor).
3. **OAuth consent screen** → External → uygulama adı verin → **Test users** bölümüne
   kendi Gmail adresinizi ekleyin.
4. **Credentials → Create Credentials → OAuth client ID** → tür: **Desktop app** →
   oluşan JSON'u indirip `credentials/client_secret.json` olarak kaydedin.
5. İlk `/baslat` çalıştığında tarayıcı açılır, Drive erişimine (salt okuma) izin verirsiniz;
   token `credentials/token.json`'a kaydedilir ve otomatik yenilenir.

### 4. Telegram botu

1. Telegram'da **@BotFather**'a `/newbot` yazıp bot oluşturun, token'ı alın.
2. Kendi sayısal user ID'nizi öğrenin: **@userinfobot**'a herhangi bir mesaj atın.

### 5. .env dosyası

```bash
cp .env.example .env   # Windows: copy .env.example .env
```

`.env` içini doldurun:

```
TELEGRAM_BOT_TOKEN=123456:ABC...
ALLOWED_USER_IDS=123456789
DRIVE_ROOT_FOLDER_ID=18v81QssO2Iu9UhjAvNAOVOqexjUvievT
DEFAULT_THUMBNAIL=data/images/default_thumbnail.jpg
TEMP_DIR=data/temp
BROWSER_PROFILE_DIR=data/browser_profile
HEADLESS=false
UPLOAD_TIMEOUT_MINUTES=30
```

`data/images/` klasörüne **2 default görsel** (videoda görünecek) ve
**`default_thumbnail.jpg`** (YouTube küçük resmi) kopyalayın. Bot açılışta bu klasörü
tarayıp görselleri kütüphaneye ekler.

### 6. Google oturumunu bir kez açın (Playwright profili)

```bash
python -m bot --login-setup
```

Görünür bir Chrome açılır:

1. Google hesabınıza giriş yapın ve **studio.youtube.com**'un açıldığını görün.
2. Sağ üst avatar → hesap/kanal değiştirmeyi deneyip **iki kanal arasında geçişin**
   çalıştığını doğrulayın.
3. Terminale dönüp **Enter**'a basın — oturum `data/browser_profile/` içine kaydedilir.

### 7. Kanal adlarını kaydedin

Botu başlattıktan sonra Telegram'dan `/kanallar` → **➕ Kanal ekle** ile iki kanalınızın
**Studio'daki görünen adını birebir** yazın (büyük/küçük harf dahil aynı olmalı).
Playwright kanal değiştirirken bu ada göre eşleştirme yapar.

### 8. Botu başlatın

```bash
python -m bot
```

Telegram'dan `/start` → `/baslat` ile ilk yüklemeyi yapın. İlk yüklemeler için
`HEADLESS=false` bırakın ki tarayıcıda olan biteni izleyebilin; birkaç sorunsuz
yüklemeden sonra `HEADLESS=true` yapabilirsiniz.

### Arka planda sürekli çalıştırma

**Windows (Görev Zamanlayıcı):**

1. `pythonw` ile konsolsuz başlatmak için bir `start_bot.bat` oluşturun:
   ```bat
   @echo off
   cd /d C:\yol\telegram-yt-bot
   pythonw -m bot
   ```
2. Görev Zamanlayıcı → Temel Görev Oluştur → Tetikleyici: *Oturum açıldığında* →
   Eylem: bu .bat dosyası. "Yalnızca kullanıcı oturum açtığında çalıştır" seçili kalsın.
3. **Not:** Playwright **headful** modda çalışacaksa oturumu açık bir masaüstü gerekir
   (kilitli/oturumsuz makinede pencere açılamaz). Bu yüzden Windows'ta kararlı
   çalıştıktan sonra `HEADLESS=true` önerilir.

**Linux (systemd):** `/etc/systemd/system/yt-bot.service`:

```ini
[Unit]
Description=Telegram Drive-YouTube Bot
After=network-online.target

[Service]
Type=simple
User=KULLANICI
WorkingDirectory=/home/KULLANICI/telegram-yt-bot
ExecStart=/usr/bin/python3 -m bot
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now yt-bot
```

---

## Kullanım Akışı (özet)

1. `/baslat` → "🔍 Drive taranıyor..." → yeni klasörler listelenir (yoksa son tarama
   zamanı + 📜 Geçmiş).
2. Klasör seç → kanal seç → görsel seç (📚 kayıtlılardan veya 📤 telefondan gönder;
   gönderilen görsel istenirse kütüphaneye kaydedilir).
3. Başlık (klasör adı önerilir) ✅/✏️ → açıklama (Word'ün tamamı) ✅/✏️.
4. Son onay kartı → 🚀 Yükle.
5. Bot sırasıyla sesi indirir, videoyu üretir, Studio'ya yükler; kritik anlarda ekran
   görüntüsü gönderir → "✅ Yayınlandı! {link}".
6. Hata olursa: Türkçe açıklama + hata anı ekran görüntüsü + 🔁 Tekrar dene.

## Önemli Uyarılar

- **`data/browser_profile/` Google oturumunuzu içerir.** Bu klasörü asla paylaşmayın,
  yedeğini herkese açık yere koymayın, commit etmeyin (`.gitignore`'da). Ele geçiren
  kişi YouTube/Google hesabınıza erişebilir.
- `.env` ve `credentials/` da aynı şekilde gizlidir ve `.gitignore`'dadır.
- **Studio arayüzü değişirse** selector güncellemesi gerekebilir. Tüm selector'lar
  `bot/services/studio_uploader.py` başındaki `SELECTORS` sözlüğünde toplanmıştır —
  tek yerden düzeltilir.
- **Özel thumbnail** için kanalın **telefon doğrulaması** yapılmış olmalıdır
  (studio.youtube.com → Ayarlar → Kanal → Özellik uygunluğu). Doğrulanmamışsa bot bu
  adımı atlar ve Telegram'dan bilgi verir.
- Google oturumu düşerse bot size "🔑 Google oturumu düşmüş..." bildirimi gönderir;
  bilgisayarda `python -m bot --login-setup` çalıştırıp yeniden giriş yapın.
- **`HEADLESS=true` sorunlu olabilir:** Google, headless tarayıcıyı yeni cihaz sanıp
  her açılışta "Kimliğinizi doğrulayın" diyaloğu çıkarabiliyor (bot bu durumu algılar
  ve Türkçe hata verir). Bu yaşanırsa `HEADLESS=false` kullanın.

## Sorun Giderme

| Belirti | Çözüm |
|---|---|
| "ffmpeg bulunamadı" | ffmpeg'i kurun, terminali yeniden açın, `ffmpeg -version` doğrulayın |
| "client_secret.json bulunamadı" | Google Cloud adımını tamamlayıp dosyayı `credentials/` içine koyun |
| "Google oturumu düşmüş" | `python -m bot --login-setup` ile yeniden giriş yapın |
| "'X' adlı kanal bulunamadı" | `/kanallar`'daki ad Studio'daki görünen adla birebir aynı mı kontrol edin |
| Yükleme bir adımda takılıyor | Gönderilen ekran görüntüsüne bakın; gerekirse `SELECTORS` sözlüğünü güncelleyin |
| Bot yetkisiz diyor / cevap yok | `.env`'deki `ALLOWED_USER_IDS` kendi sayısal ID'niz mi kontrol edin |

## Proje Yapısı

```
bot/
├── __main__.py            # giriş noktası (+ --login-setup)
├── config.py              # .env yükleme/doğrulama
├── states.py              # FSM state'leri
├── middlewares/auth.py    # whitelist güvenliği
├── handlers/              # /start /baslat /istatistik /gorseller /kanallar
└── services/
    ├── drive.py           # Drive API v3 (readonly) + retry
    ├── video.py           # ffmpeg 1080p mp4 üretimi
    ├── docx_reader.py     # Word → düz metin (paragraf + tablo)
    ├── studio_uploader.py # Playwright: Studio yükleme (projenin kalbi)
    └── db.py              # SQLite: klasörler, kanallar, görseller, yüklemeler
data/                      # bot.db, images/, browser_profile/, temp/ (gitignore)
credentials/               # client_secret.json, token.json (gitignore)
logs/                      # günlük dönen log dosyaları (gitignore)
```
