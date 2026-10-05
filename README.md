# Verda v2

Mac üzerinde çalışan yeni araştırma motorunun ilk parçası. Mevcut GitHub Pages arayüzü korunur. Bu depo henüz tam agent uygulaması değildir.

Kod deposu: [tremo/verda](https://github.com/tremo/verda). Gerçek ilan verileri, satıcı yazışmaları, yerel raporlar ve erişim anahtarları bu depoya dahil değildir.

Bu sürüm:

- Eski SQLite verisini tek tutarlı, salt okunur işlemle alır; kaynak veritabanını değiştirmez.
- İlan, kontrol, görev, kapı, mesaj, kullanıcı notu ve olay geçmişini sürümlü bir gölge kopyada saklar.
- Aynı içerik tekrar aktarıldığında yeni kayıt üretmez; değişen kaynak ayrı sürüm oluşturur.
- Hazır mesajları ve eski görevleri çalıştırılabilir kuyruğa koymaz. Belirsiz teslimatı aynen korur.
- İlan dosyası ve sayfalanmış işlem geçmişini yerel API’den sunar.
- Yeni doğrulanmış gözlemler için 15 milyon TL, 1.000 m² ve 45 dk kurallarını hesaplar.
- Mevcut dashboard JSON’unu `schemaVersion: 2` sınırında doğrular; bilinmeyen alanları kaybetmeden saklar ve geri verir.
- Araştırma görevlerini bağımlılık sırasına göre kalıcı olarak kaydeder; kaynak kilidi, süreli görev sahipliği, yeniden deneme ve iptal uygular.
- Dokuz adımlı örnek araştırmayı sentetik verilerle baştan sona çalıştırır. Gerçek kaynak bağlantıları eksikse işi açık bir nedenle bekletir.

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

## Kalıcı görev motoru

Eski verinin arşivi `.local/verda.sqlite`, yeni görev kuyruğu `.local/workflows.sqlite` içinde tutulur. `verda init` iki veritabanını kurar; aynı komut tekrar çalıştırılabilir. Eski görevler otomatik olarak yeni kuyruğa aktarılmaz. İki veritabanını aynı dosyaya yönlendirmeyin; şema kontrolü bunun kabul edilmesini engeller. Yedekte her iki dosya da yer almalıdır.

Standart araştırma sırası:

```text
İlanı oku → parsel kimliğini çöz → TKGM kimlik/geometri doğrulaması
  → doğal sit / arkeolojik sit / erişim / rota / rakım
  → değerlendirme
```

Bu sırayı şu anda kurallı planlayıcı oluşturur; yönetici için model döngüsü henüz eklenmedi. Plan dışı yetkiler, döngüsel bağımlılıklar ve parsel adımı olmadan mekânsal kontrol isteyen planlar reddedilir. Gerçek araştırmanın ucuz ön eleme, eksik kimlik sorusu, yanıt bekleme ve kanıt kabul dalları bu sabit örnek plana henüz eklenmedi.

Sentetik senaryoyu çalıştırmak için:

```bash
.venv/bin/verda demo-run
.venv/bin/verda workflow-status RUN_ID
```

`demo-run` çalıştırma kimliğini, dokuz görevi ve olay geçmişini döndürür. Aynı istek anahtarıyla yeniden çalıştırıldığında tamamlanan adımlar tekrarlanmaz. Yeni örnek için `demo-run --request-key another-demo` kullanılabilir. Örnek sonuçlar `demo: true` olarak işaretlenir; gerçek ilan değerlendirmesi değildir ve `shadow` işini tamamlayamaz.

Aktarılan bir aday için yetkili yerel istemci üzerinden yeni, gölge araştırma planı açılabilir:

```text
POST /api/workflows/from-case/{snapshot}/{listing}
Body: {"request_key": "unique-request-key"}

GET /api/workflows/{run_id}?after_event_id=0&limit=100
POST /api/workflows/{run_id}/cancel
```

API planı kaynak sürümüne bağlar. Tekrarlanan istek anahtarı aynı çalışmayı döndürür; farklı planla tekrar kullanılırsa hata verir. Sistem tarafından elenmiş ilanı yeniden araştırmak ayrıca yeniden değerlendirme kararı gerektirir. Yazma uçları şu aşamada yalnız Bearer anahtarlı ve `Origin` başlığı olmayan yerel istemciler içindir; mevcut dashboard bu API’ye henüz bağlanmadı.

`workflow-step --mode shadow` sıradaki bir işi alır. Canlı adaptör olmadığı için `connector_not_implemented` nedeniyle bekletir; başarı veya kanıt üretmez. `workflow-cancel RUN_ID` bekleyen ve çalışan görevleri iptal eder; tarihçeyi ve tamamlanmış sonuçları silmez.

Yerel yürütme özellikleri:

- Aynı SQLite dosyasını kullanan çalışanlar işi kısa, atomik bir işlemle alır. Bir görevi yalnız bir çalışan sahiplenebilir; aynı kaynağı kullanan iki görev aynı anda sahiplenilemez.
- Çalışan kaybolursa süreli sahiplik sona erer. Bir sonraki kuyruk sorgusu işi artan beklemeyle yeniden denemeye açar; varsayılan sınır üç denemedir.
- Süresi dolmuş veya iptal edilmiş işe geç gelen sonuç reddedilir. Uzun adımlar `heartbeat` ile sahipliği uzatabilir.
- Başarısız ya da bekleyen önkoşul, ona bağlı adımları başlatmaz. Durum yanıtında hangi adıma bağlı bekledikleri, hata nedeni, deneme sayısı ve olaylar bulunur.

Bu kuyruğun PostgreSQL uygulaması henüz yok; yerel eşzamanlılık testleri SQLite üzerinde çalışır. Süreli sahiplik veritabanına sonuç kabulünü korur; daha önce başlamış harici tarayıcı işlemini fiziksel olarak durdurma garantisi vermez. Canlı adaptörlerden önce tek tarayıcı yöneticisi, kanıt doğrulayıcı ve gönderimler için ayrı teslimat uzlaştırması eklenmelidir. Bu sürümde gönderim aracı bulunmaz.

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

Testler sentetik SQLite, geçici dosyalar ve yerel HTTP istemcisi kullanır. Kaynak değişmezliği, tekrar aktarım, geçmişin ve mesaj durumlarının korunması, yetkisiz erişim, politika eşikleri ve dashboard alanlarının kaybolmaması kontrol edilir. Görev motorunda eşzamanlı sahiplenme, süre aşımı, yeniden başlama, deneme sınırı, bağımlılıklar, iptal ve örnek/gerçek iş ayrımı ayrıca test edilir.

Agent kataloğu bu sürümde yetenek sözleşmesidir. Kalıcı görev yürütücüsü ve sentetik worker çalışır. LLM döngüsü, Sahibinden/TKGM adaptörleri, gerçek kanıt kabulü, canlı gönderici ve otomatik Firestore yayını sonraki parçalardır. Hazır oldukları iddia edilmez.
