# Verda v2

Mac üzerinde çalışan yeni araştırma motorunun ilk parçası. Mevcut GitHub Pages arayüzü korunur. Bu depo henüz tam agent uygulaması değildir.

Bu sürüm:

- Eski SQLite verisini tek tutarlı, salt okunur işlemle alır; kaynak veritabanını değiştirmez.
- İlan, kontrol, görev, kapı, mesaj, kullanıcı notu ve olay geçmişini sürümlü bir gölge kopyada saklar.
- Aynı içerik tekrar aktarıldığında yeni kayıt üretmez; değişen kaynak ayrı sürüm oluşturur.
- Hazır mesajları ve eski görevleri çalıştırılabilir kuyruğa koymaz. Belirsiz teslimatı aynen korur.
- İlan dosyası ve sayfalanmış işlem geçmişini yerel API’den sunar.
- Yeni doğrulanmış gözlemler için 15 milyon TL, 1.000 m² ve 45 dk kurallarını hesaplar.
- Mevcut dashboard JSON’unu `schemaVersion: 2` sınırında doğrular; bilinmeyen alanları kaybetmeden saklar ve geri verir.

Dashboard adaptörü şimdilik **mevcut üretilmiş görünümü taşıma** katmanıdır. Yeni agent sonuçlarından tam dashboard üretimi, Firestore senkronizasyonu ve canlı yayın henüz uygulanmadı. `sourceUpdatedAt` güncelliğini yeniden kanıtladığı iddia edilmez. Aktarılan tarihçe de yeniden doğrulanmış kanıt sayılmaz.

## Yerel çalıştırma

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test,postgres]'
.venv/bin/verda init
.venv/bin/verda import-legacy /absolute/path/to/datca-state-backup.sqlite
.venv/bin/verda serve
```

Bu çalışma sırasında doğrulanan bağımlılık sürümleri `requirements.lock` içindedir. Aynı ortamı kurmak için önce `.venv/bin/pip install -r requirements.lock`, ardından `.venv/bin/pip install --no-deps -e .` kullanılabilir.

Yerel adres `http://127.0.0.1:8765/health`. Veri API’leri `.local/api-token` dosyasındaki anahtarı `Authorization: Bearer ...` başlığında ister. Anahtar ve bütün yerel veriler Git dışında kalır. Sunucu dış ağa bağlanmaz; satıcı mesajı ve bulut yayını yapacak bir endpoint içermez. API yeni bir dashboard değildir.

Başlıca okumalar:

```text
GET /api/snapshots
GET /api/snapshots/{snapshot}/cases
GET /api/snapshots/{snapshot}/cases/{listing}/events
GET /api/agents
GET /api/policy
POST /api/policy/preview
GET /api/dashboard/{projection}
```

Gölge aktarımdaki `migration_review` bir araştırma sonucu değil, yeni yürütme motoruna geçmeden önce uzlaştırma gerektiği anlamına gelir. Olaylar kaynak sıra numarasıyla sayfalanır; istemci `next_after_id` ile devam eder. Kaynak tabloları ve JSON’ları ayrıca kayıpsız saklanır. İlan dışı eski olaylar da kaynak arşivinde kalır.

SQLite bu ilk aşamanın yerel geliştirme deposudur. PostgreSQL bağlantısı SQLAlchemy üzerinden hazırdır; canlı çoklu worker aşamasında PostgreSQL/PostGIS, sürümlü şema migrasyonları ve gerçek eşzamanlılık testleri eklenecek. PostgreSQL/PostGIS işletimi bu ilk teslimatta doğrulanmış değildir. `init` yalnız boş v1 şemayı kurar; sonraki şema yükseltmeleri için migration gerekir.

## Arayüz uyumluluğu

GitHub Pages statik arayüzü barındırır; paketteki istemci özel Firestore koleksiyonlarını okur. V2 aynı koleksiyon/sözleşme biçimini üretecek. Mevcut HTML, CSS ve kullanıcı akışını değiştirmek bu aşamanın kapsamı değildir. Yerel API’yi uzaktaki Pages sayfasının doğrudan çağırması varsayılmıyor.

Canlı [dashboard](https://tremo.github.io/datca-arsa-dashboard/) ve [kaynak depo](https://github.com/tremo/datca-arsa-dashboard) 5 Ekim 2026’da kontrol edildi. İncelenen kaynak sürümü: `dd55b3a57e3e6edd9b63d5870463fadb3d25f033`. Ana ekrandaki araştırma özeti, bekleyen işler ve ilan kartları mevcut yetkili oturumla görüntülendi. Kaynakta Harita ve Karşılaştır sayfaları da bulunuyor. Hiçbir arayüz dosyası değiştirilmedi.

Canlı `client.js` ile doğrulanan bağlantı sözleşmesi:

| Veri | Mevcut istemcinin beklediği biçim | V2 geçiş kuralı |
|---|---|---|
| `meta/status` | `schemaVersion: 2`, `recordCount`, `generatedAt`, `sourceUpdatedAt`; özet ve yöntem alanları | Kaynak gözlem zamanı yayın zamanı ile değiştirilmez; yayın özeti veri hazır olunca güncellenir |
| `listings/{id}` | `dashboardIncluded == true` sorgusu; mevcut kart, harita, kanıt ve aşama alanları | Mevcut belge kimlikleri ve alan anlamları korunur |
| `listingFeedback/{id}` | Karar, not, `userFacts`, `questions`; alan bazında birleştirme | Kullanıcı kararları geri alınmaz; cevaplar yalnız ilgili soru alanlarına yazılır |
| `userState/{name}` | Görülen ilanlar ve karşılaştırma tercihleri | Araştırma motoru bu koleksiyonu yayımlayıp üzerine yazmaz |
| `evidenceAssets/{id}` | `mime` ve `chunks`; JPEG/PNG/WebP | Kanıt kimlikleri korunur; yalnız yetkili veri katmanında bulunur |

Bu tablo uygulanan Firestore senkronizasyonu anlamına gelmez. İlk sürümde yalnız yerel aktarım ve veri sözleşmesi sınırı çalışır. Canlıya bağlanmadan önce mevcut istemciyle veri üretimi, önbellek yenilenmesi, eksik yayın ve kullanıcı notlarının korunması uçtan uca doğrulanmalıdır.

Mevcut üretilmiş JSON varsa:

```bash
.venv/bin/verda import-dashboard /absolute/path/to/data.json
.venv/bin/verda export-dashboard PROJECTION_ID .local/dashboard-copy.json
```

Bu çıktı özel veridir; Pages deposuna doğrudan eklenmez. Export mevcut dosyanın üzerine yazmaz. Yayın, sonraki aşamada mevcut özel veri katmanına ayrı bir adaptörle yapılacak.

## Doğrulama

```bash
.venv/bin/pytest -q
```

Testler sentetik SQLite, geçici dosyalar ve yerel HTTP istemcisi kullanır. Kaynak değişmezliği, tekrar aktarım, geçmişin ve mesaj durumlarının korunması, yetkisiz erişim, politika eşikleri ve dashboard alanlarının kaybolmaması kontrol edilir.

Agent kataloğu bu sürümde yetenek sözleşmesidir. LLM döngüsü, Sahibinden/TKGM bağlantısı, görev yürütücüsü, canlı gönderici ve otomatik Firestore yayını sonraki parçalardır. Hazır oldukları iddia edilmez.
