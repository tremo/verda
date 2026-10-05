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
- Model gerektiren adımları agent bazında seçilen sağlayıcıya yönlendirir. İlk çalışan sağlayıcı Codex CLI’dir; kural hesapları ve kaynak adaptörleri model çağırmaz.

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

### Agent yönetim ekranı

Sunucu çalışırken `http://127.0.0.1:8765/control` adresini açın. Bu ayrı yerel ekran mevcut ilan dashboard’ını değiştirmez. Yedi uzmanın yetkinlikleri, sınırları, agent bazında model profili, işlerin kod/adaptör/model ayrımı ve yönergeler görünür. Yönetici prompt’u gerçek öneri çağrısıyla aynı kaynaktan gelir; diğer yönergeler henüz taslaktır. Ekran salt okunurdur; prompt veya model düzenleme henüz yoktur.

Sahibinden bölümünde tek seçilmiş Mac tarayıcı oturumu hedefi ve başlangıç hız önerileri yer alır. Tarayıcı köprüsü, hız sınırlayıcı ve engel algılama henüz canlı kaynağa bağlanmadığı için açıkça bu durum gösterilir. 180 saniye aralık, 5 işlik grup ve 30 dakika mola, sitenin onayladığı veya engellemeyeceği sınırlar değildir. CAPTCHA/429/403/oturum değişiminde kaynak kuyruğunun durması ve otomatik engel aşma yapılmaması hedef davranıştır.

`/control` ve `/control/catalog` yalnız uygulama tanımlarını sunar; gerçek ilan, yazışma veya erişim anahtarı içermez ve model çağırmaz. Özel veri API’leri Bearer korumasını sürdürür. Sunucu loopback üzerinde çalışır, beklenmeyen Host başlıklarını reddeder. Bu ekranı olduğu gibi internete açmak desteklenmez.

### İlan akışları

`/control/flows` ekranında ilan seçimi, eski araştırma geçmişi ve varsa ilana bağlı V2 araştırmaları bulunur. Agent düğümünü seçince aldığı girdi, ürettiği sonuç, sonraki işler, bekleme nedeni, deneme sayısı ve olay geçmişi görünür. Dallanan görevler aynı grafikte gösterilir. Düz çizgi kaydedilmiş veri aktarımını, kesik çizgi yalnız plan bağımlılığını gösterir.

Görev sahiplenildiğinde çalışana verilen bağımlılık sonuçlarının o andaki kopyası olay kaydına yazılır. Sonradan güncel sonuçlardan geçmiş girdi uydurulmaz. Önceki sürümdeki çalışmalar ve eski araştırma arşivi bu kayıtları içermiyorsa ekran bunu açıkça belirtir. Bu görünüm işlem kayıtlarını gösterir; modelin iç düşünce sürecini göstermez. Model/prompt sürümü kaydedilmemişse bilinmiyor olarak kalır.

Gerçek geçmiş özeldir. `verda serve`, `.local/viewer-link` dosyasına yalnız dosya sahibinin okuyabildiği bir bağlantı yazar. Sunucuyu başlattıktan sonra bu bağlantıyı aynı Mac'in tarayıcısında açın: 10 dakika geçerli tek kullanımlık bağlantı, dört saatlik salt okunur oturum açar. Sonra `/control/flows` kullanılabilir. Sunucu yeniden başladığında yeni bağlantı gerekir. Bağlantıyı paylaşmayın. Tarayıcı oturumu kuyruk oluşturma, iptal veya diğer yazma işlemleri için yetki vermez; mevcut API Bearer koruması devam eder. Yanıtlar önbelleğe alınmaz.

“Örnek V2 akışları” yalnız sentetik çalışmaları listeler. Gerçek ilana otomatik olarak demo bağlanmaz. Canlı kaynak adaptörleri henüz tamamlanmadığından gerçek ilanların eski olayları görülebilir, ancak bunlar yeni agent akışı gibi sunulmaz. Yeni bir sentetik örnek `verda demo-run --request-key example-trace` ile oluşturulabilir. Ekran Yenile düğmesiyle güncellenir; otomatik canlı takip henüz yoktur. Mevcut GitHub Pages dashboard'ı değiştirilmez.

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

Bu sırayı şu anda kurallı planlayıcı oluşturur. Codex ile yönetici önerisi ayrı olarak çalışır; öneriyi otomatik kabul eden bir agent döngüsü henüz eklenmedi. Plan dışı yetkiler, döngüsel bağımlılıklar ve parsel adımı olmadan mekânsal kontrol isteyen planlar reddedilir. Gerçek araştırmanın ucuz ön eleme, eksik kimlik sorusu, yanıt bekleme ve kanıt kabul dalları bu sabit örnek plana henüz eklenmedi.

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

## Codex, diğer sağlayıcılar ve modelsiz işler

Bir uzmanlık rolü sürekli çalışan bir model oturumu değildir. Yürütme türü her işlem için ayrı tanımlanır:

| İş | Yürütme |
|---|---|
| Yönetici önerisi, soru taslağı, karar açıklaması | Model çağrısı |
| Belirsiz erişim kanıtını yorumlama | Model desteği; sonuç ayrıca doğrulanmalı |
| Fiyat/alan eşikleri, uygunluk kuralları | Normal Python kodu |
| İlan/konuşma okuma, TKGM sorgusu, sit katmanı, rota ve rakım sağlayıcısı | Kaynak adaptörü; model çağrısı değil |
| Görev sıralama, kilitler, tekrar deneme, veri aktarımı | Normal Python kodu |

Kaynak adaptörlerinin canlı bağlantıları henüz tamamlanmadı. Bu ayrım onların uygulanmış olduğu anlamına gelmez. Model tercihi değiştirildiğinde statik bir iş modele dönüşmez. Örneğin `assessment.assess` kural motorunu çalıştırır, aynı uzmanın `assessment.explain` adımı seçilen modelle açıklama üretebilir. Model çıktısı kendiliğinden kanıt, karar veya gönderim talimatı olmaz.

İlk sağlayıcı `codex exec` kullanır. Codex kurulu ve kendi hesabıyla giriş yapmış olmalıdır. Verda oturum anahtarını okumaz/kopyalamaz. Bağlantı, bu Mac’te mevcut ChatGPT oturumuyla yalnız sentetik veri kullanılarak doğrulandı. Verda ayrıca OpenAI API anahtarı istemez; Codex hesabının kendi erişimi ve kullanım limitleri geçerlidir. Resmî kaynaklar: [kimlik doğrulama](https://learn.chatgpt.com/docs/auth), [etkileşimsiz kullanım ve JSON çıktısı](https://learn.chatgpt.com/docs/non-interactive-mode).

```bash
# Hangi işin model, hangisinin kod/adaptör olduğunu gösterir; model çağırmaz.
.venv/bin/verda runtime-info

# Yalnız sentetik özetle gerçek bir Codex çağrısı yapar; kuyrukta iş başlatmaz.
.venv/bin/verda manager-demo

# Tercihler yerel dosyada saklanabilir.
cp runtime.example.toml .local/runtime.toml
.venv/bin/verda --runtime-config .local/runtime.toml manager-demo
```

Varsayılan sağlayıcı `codex`tir. `model` belirtilmezse kurulu Codex CLI’nin varsayılanı kullanılır; belirli bir model isteniyorsa `runtime.toml` içinde yazılır. Genel ayarlardaki belirtilmemiş alanlar agent profiline aktarılır. Verda model çağrılarında kullanıcı Codex yapılandırmasını yüklemez; model tercihini bu dosyadan açıkça verin.

```toml
[default]
provider = "codex"
timeout_seconds = 120

[agents.manager]
provider = "codex"
# model = "hesabinda-desteklenen-model"

# Başka sağlayıcının adaptörü kaydedildikten sonra:
# [agents.sahibinden]
# provider = "another_vendor"
# model = "secilen-model"
```

Farklı firma için `ModelProvider.generate(...)` arayüzünü uygulayan adaptör yazılır ve `Runtime(..., providers={"codex": CodexProvider(), "another_vendor": adapter})` ile kaydedilir. İş akışını veya diğer agent’ları değiştirmek gerekmez. Şu anda yalnız Codex adaptörü uygulanmıştır; başka firma adını ayara yazmak tek başına bağlantı kurmaz. Kayıtsız sağlayıcı veya hatalı model açık hata verir; sessiz sağlayıcı/model değişimi yapılmaz.

Codex çağrısı geçici bir çalışma dizininde, salt okunur modda, komut ve tarayıcı araçları kapalı olarak yapılır. İstek standart girdiden verilir; sonuç JSON şemasıyla doğrulanır. Zaman aşımı, bağlam/çıktı boyutu sınırı ve işlem sonlandırma uygulanır. Sonuçta sağlayıcı, istenen model, çağrı kimliği, süre ve Codex’in bildirdiği token kullanımı yer alır; tahmini ücret üretilmez. Geçici dosyalar temizlenir. Bu makinede doğrulanan CLI sürümü `0.155.0-alpha.9.2`dir; eski sürümlerde desteklenmeyen seçenekler izinleri genişleterek tekrar denenmez.

`manager_preview` yalnız uygulamanın verdiği yapılabilir adımlar arasından öneri kabul eder. Sonuç `advisory_only: true` olarak döner. Bu model katmanı henüz gerçek ilan kuyruğuna, canlı mesajlara veya dashboard’a otomatik bağlanmadı; tam araştırma döngüsü tamamlanmış değildir.

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

Agent kataloğu bu sürümde yetenek sözleşmesidir. Kalıcı görev yürütücüsü, sentetik worker, Codex ile tek çağrılık yönetici önerisi ve sağlayıcı yönlendirmesi çalışır. Tam model/araç döngüsü, Sahibinden/TKGM adaptörleri, gerçek kanıt kabulü, canlı gönderici ve otomatik Firestore yayını sonraki parçalardır. Hazır oldukları iddia edilmez.
