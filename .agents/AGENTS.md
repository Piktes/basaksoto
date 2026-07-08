# Basaksoto Projesi Geliştirme Kuralları

Bu proje, yerel bir Windows makinesi (geliştirici alanı) ve uzak bir Linux sunucusu (botun çalıştığı alan) üzerinde ortaklaşa yürütülmektedir. Bu yapıda geliştirme yaparken aşağıdaki kurallara kesinlikle uyulmalıdır:

## 1. Google Drive API (OAuth) Yetkilendirme Akışı
- **Kural:** Google Drive API erişimi için gereken `token.json` yetkilendirmesi kesinlikle **sunucu üzerinde (VNC veya CLI) çalıştırılmamalıdır.** Sunucuda arayüz tarayıcısı bulunmadığından akış hata verir veya tıkanır.
- **Yöntem:** Yetkilendirme akışı yerel makinede (`c:\Users\sahin\.gemini\antigravity\scratch\Basaksoto`) çalıştırılır. Kullanıcı kendi yerel tarayıcısından izinleri verdikten sonra oluşan `credentials/token.json` dosyası `scp` ile sunucudaki `/home/basaksoto/credentials/token.json` konumuna yüklenir.
- *(Not: Playwright ile yapılan YouTube Studio oturum açma işlemi (`--login-setup`) sunucu üzerinde TightVNC/DISPLAY ile çalıştırılmaya devam edebilir.)*

## 2. Git ve Versiyon Kontrolü
- **Kural:** Yerel değişiklikleri GitHub'a pushlamadan önce, her zaman remote reposundaki en son commit geçmişini kontrol edin. Asla körlemesine `git push --force` yapmayın. Remote'daki commitlerin localde mevcut olduğundan emin olun.

## 3. Veritabanı (db.py) ve Kod Bütünlüğü
- **Kural:** Dosyaları localden kopyalarken veya güncellerken, `db.py` gibi kritik dosyaların içindeki yeni eklenmiş fonksiyonların (`get_all_channel_names`, `get_all_folder_uploads`, tag yönetimi vb.) kaybolmadığından emin olun. Gerekirse kopyalama yapmak yerine kod değişikliklerini parça parça uygulayın.
