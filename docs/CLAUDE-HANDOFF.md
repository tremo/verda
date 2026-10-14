# Verda geliştirme devir notu

6 Ekim 2026. Claude veya başka bir geliştirme asistanının mevcut çalışmayı devam ettirmesi için hazırlanmıştır. Kod tabanı `866bd3f` commit'indedir. Verda'nın agent çekirdeği, yönetim paneli ve Jev üzerinden ilk canlı Sahibinden araması çalıştırıldı. Mesajlaşma ve tam ilan araştırması henüz tamamlanmadı.

## Proje ve çalışma yeri

- Git deposu: https://github.com/tremo/verda
- Gerçek yerel depo: `/Users/m/Dev/Verda/v2`. Üstteki `/Users/m/Dev/Verda` depo kökü değildir.
- Dal: `main`.
- Mevcut ilan dashboardı: https://tremo.github.io/datca-arsa-dashboard/
- Eski dashboard deposu: https://github.com/tremo/datca-arsa-dashboard
- Eski dashboard yerel kopyası: `/Users/m/Dev/Verda/current-dashboard`.
- Yeni yönetim paneli: `http://127.0.0.1:18767/control`.

Eski dashboardın arayüzü korunacak. V2, agent ve iş yönetimini ekleyen ayrı uygulamadır. Eski dashboardın özel verisi Firestore üzerinden gelir; GitHub Pages dosyaları tüm araştırma verisini içermez.

## Kullanıcının istediği mimari

Kullanıcı n8n benzeri, genişletilebilir bir agent çatısı istiyor. Agent, görev, araç ve tetikleyici ayrı kavramlardır. Örneğin “doğal sit kontrolü” bir agent değildir; Sahibinden operatörüne verilen ve mesajlaşma araçlarıyla yapılan bir görevdir.

Sahibinden operatörü sabah zamanlayıcısından arama işi, yöneticiden ilan ayrıntısı okuma işi veya farklı ilanlar için mesaj gönderme işleri alabilir. Bunlar aynı operatörün kalıcı kuyruğunda sıralanır. Her araştırmayı zorunlu sabit aşamalardan geçiren bir pipeline'a geri dönülmemeli.

Ana yönetici hedefleri takip eder, kayıtları ve eksikleri inceler, uygun operatöre görev verir, sonuçları değerlendirir ve gerekli sonraki işi seçer. Araç çalıştırma, doğrulama, veri kaydı ve basit hesaplar gerektiğinde statik kodla yapılır. Her işlem model çağrısı gerektirmez.

Ortak kayıt servisi araç kanıtlarını ve sonuçları saklar. Her yazma işlemini yöneticinin bizzat yapması gerekmiyor. Sonuç hem işi isteyen tarafa hem yöneticiye uygun teslim mekanizmasıyla döner; aynı yöneticiye yinelenen bildirim veya sonsuz görev döngüsü üretilmemeli. Kayıt, sonuç teslimi ve kaynak hatası ayrı izlenir.

Panelde agent'ın kalıcı yönergesi, araçları, görevleri ve gerçek çalışma akışı görülebilmeli. Yetki bağlantısı ile gerçekten gerçekleşmiş görev aktarımı farklı gösterilmeli. Kullanıcı ayrıca zamanlayıcının hangi saatte, hangi kapsam ve yönergeyle kimi tetiklediğini görebilmek istiyor.

## Kesinleşmiş ürün kararları

- İlk sürüm kullanıcının Mac'inde, yalnız kendi Datça araştırması için çalışacak; sonradan taşınmaya ve genişlemeye uygun olacak.
- Arama kriterleri: en fazla 15 milyon TL, en az 1.000 m², Palamutbükü Mertur Tatil Sitesi'ne araçla en fazla 45 dakika. Eşiği aşanlar ayrı; eşik çevresindeki çelişkili ölçümler koşullu gösterilecek.
- Mertur'un kesin giriş noktası ve canlı rota entegrasyonu tamamlanmış kabul edilmemeli. Kullanıcı önce bizim bulmamızı, bulunamazsa kendisinin paylaşmasını istedi.
- Chrome kullanıcının mevcut tarayıcısıdır. Profil veya çerez kopyalanmaz. Kullanıcı remote debugging iznini kendisi açtı ve bağlantı daha önce çalıştı.
- Kullanıcı belirlediği kurallar içinde otomatik mesaj gönderilmesini, istisnaların kendisine sorulmasını istiyor. Ancak canlı gönderim aracı ve ayrıntılı gönderim politikası henüz bağlı değil.
- Önce eski yazışmalar ve cevaplar okunmalı; cevaplanmış soru tekrar sorulmamalı. Eski notlar, dışlanan ilanlar ve belirsiz gönderimler korunmalı.
- Sit için ilk sürümde satıcının mesajdaki açık beyanı kabul ediliyor. Genel soruya açık “sit yok” yanıtı gelirse doğal ve arkeolojik sit açısından ilk elemede yok kabul edilir. Yalnız bir türe verilen cevap diğerine genellenmez.
- Etiket “Satıcı beyanına göre sit yok” olmalı. Resmî doğrulama denmemeli. Soru, yanıtın aynen metni, ilan ve konuşma/mesaj referansları, varsa tarih tutulmalı. Eksik, belirsiz veya çelişkili cevap “yok” değildir.
- Kaynak işleri sırayla yapılır; Sahibinden araç işleri arasında en az 180 saniye vardır. CAPTCHA, hız engeli, oturum kaybı ve belirsiz gönderim durdurulur. Başka motorla engeli aşma veya kör tekrar gönderme yapılmaz.

## Tamamlananlar ve kalan sınırlar

| Alan | Durum |
| --- | --- |
| Agent çekirdeği | Kalıcı görev kuyruğu, araç yetkileri, delegasyon, model karar döngüsü, kaynak kilidi, sonuç dönüşü mevcut. |
| Ortak kayıt servisi | Gözlemler, sonuçlar, teslim kayıtları, hata olayları ve yeniden teslim mekanizmaları mevcut. |
| Yönetim paneli | Agent/araç/tetikleyici ilişkileri, iş akışları, görev ayrıntıları ve browser motor seçimi mevcut. |
| Kalıcı yönergeler | Rol ve prompt panelden düzenlenip sürümleniyor. Mevcut görevler kendi tanım anlık görüntüsünü koruyor. |
| Araştırma kaydı inceleme | `records.audit`, `records.listing` ve panelde yöneticiye inceletme mevcut. Kapsamlı otomatik arka plan tamamlama henüz yok. |
| Sahibinden arama | Jev ile canlı ilk sayfa araması uçtan uca doğrulandı. Sayfalama yok. |
| İlan ayrıntısı | Okuma kodu mevcut; Jev ile gerçek bir ilan üzerinde uçtan uca doğrulama sıradaki iş. |
| Konuşma okuma ve mesaj gönderme | Araç tanımları var, canlı adaptörler bağlı değil. Bu çalışmada satıcıya mesaj gönderilmedi. |
| TKGM ve coğrafi araştırma | Agent/araç tanımları var; canlı sorgu ve rota/rakım zinciri tamamlanmış değil. |
| Sağlayıcılar | Ana agent motorunda Codex uygulanmış. Başka şirketlerin modelleri için adaptör ve kayıt eklemek gerekiyor. |
| MCP ve script genişletme | Araç türleri için yapı var; her haricî araç için gerçek adaptör, izin, doğrulama ve test gerekiyor. |
| Görsel düzenleme | Prompt düzenleme var; grafikte serbest bağlantı düzenleme ve tüm zamanlayıcı/model ayarlarını panelden değiştirme tamamlanmadı. |
| Eski dashboarda canlı aktarım | Eski veri alma/projeksiyon parçaları var; yeni agent sonuçlarının Firestore'a güvenilir sürekli aktarımı tamamlanmadı. |
| Süreç yönetimi | Elle başlatılıyor. Mac açılışında otomatik başlatma veya süreç çökünce yeniden başlatma servisi yok. |

## Devir anındaki gerçek durum

6 Ekim 2026 saat 10.00 Türkiye saati kontrolünde yerel `serve`, `agency-worker`, `agency-jev-worker` ve Browser Harness daemon süreçleri bulunamadı. `127.0.0.1:18767/health` bağlantıyı kabul etmedi. Dolayısıyla kod ve kayıtlar mevcut, ancak uygulama bu kontrol anında açık değil. Bu not için süreçler yeniden başlatılmadı.

Yerel agency veritabanı şema sürümü 7; seçili browser motoru `jev`. Örnek sabah taraması `ornek-sabah-taramasi` kapalıdır. Otomatik günlük tarama açık sanılmamalı.

5 Ekim'deki başarılı canlı Jev çalışması: yönetici → Sahibinden operatörü → Jev → ortak kayıt → yönetici. İlk sayfadaki 20 ilan kaydedildi; site toplam 295 sonuç bildirdi. Bu, 295 ilanın tamamının okunduğu anlamına gelmez.

İzlemek için başarılı yönetici görevi `1805118fe6e74beca0a0a07fe82f1033`, operatör görevi `bd95f20ee326419ba1bd0a48fda8508c`, browser işi `3d32e5f149e64b45adcc6443b6697618`. Browser işi tamamlandı. Daha önce Codex browser sürücüsüyle de ilk arama tamamlandı.

İlk Jev denemesi site URL parametrelerinin sırasını değiştirdiği için durmuştu. Kontrol aynı sayfa ve sayısal filtre değerlerini karşılaştıracak şekilde düzeltildi; sonraki gerçek deneme başarılı oldu. Eski başarısız browser işi `8f0f356b7fa5489ebe27140bb9363557`, bağlı yönetici görevi `07cb1e71e35341e7a9fc2ace60db5824`. Bunları yeni bir kaynak hatasıyla karıştırma; geçmişi silmeden açık görevlerin nasıl kapatılacağını değerlendir. Körlemesine tekrar çalıştırma.

## Jev entegrasyonunun kapsamı

`jev-ultrafast` paketi `1231850a0bf1a0c0341fe408ef1668dbbfdfac46` upstream commit'ine, Browser Harness `0.1.13` sürümüne sabit. Yerel oturum adı `BU_NAME=verda`.

Jev kendi TypeSafe karar motorunu kullanır. Ana yönetici ve operatörlerin model sağlayıcısı bundan bağımsız olarak Codex'tir. Projeyi Claude ile geliştirmeye devam etmek çalışma zamanındaki agent modellerini otomatik olarak Claude'a dönüştürmez.

Mevcut Jev sürücüsü sınırlı okuma yapar. Arama URL'sini ve veriyi statik kod üretir/çıkarır. Model yalnız `SCROLL_UP`, `SCROLL_DOWN`, `WAIT`, `DONE`, `BLOCKED` seçebilir. Serbest tıklama, metin girişi, giriş yapma, sayfalama ve mesajlaşma yoktur. İlan ayrıntısı URL'si önceden ortak canlı kaynak kaydında görülmüş olmalı; model URL tahmin etmez.

`DONE` tek başına başarı değildir. Görünür DOM, URL, filtre, ilan kimliği, fiyat ve alan bağımsız doğrulanır; gizli satırlar elenir. Her iş en fazla 12 karar ve döngü başında denetlenen 240 saniye bütçe kullanır. Sağlayıcı çağrısı ek gecikme yaratabilir; kuyruk sahipliği 600 saniyede dolar. Belirsiz kalan sahiplenme otomatik tekrar edilmez.

TypeSafe anahtarı zaten `.local/jev.env` içinde mevcut ve dosya izni `0600`. Kullanıcıdan aynı anahtarı yeniden isteme. Değerini açığa çıkarma veya Git'e ekleme. Mevcut okuma yolu ayrı bir metin modeli anahtarına ihtiyaç duymuyor. Bu dosya başka bilgisayara Git ile taşınmaz.

Bir canlı ilk sayfa denemesinde Jev'in browser işi yaklaşık 1,53 saniye sürdü. Eski Codex denemesi farklı kurulum ve manuel filtre adımları içerdiği için doğrudan hız karşılaştırması değildir. “Jev genel olarak 109 kat hızlı” gibi bir sonuç çıkarma; kuyruk beklemesi, agent kararları ve browser süresi ayrı ölçülmeli.

Ayrıntılar: [JEV-ULTRAFAST.md](JEV-ULTRAFAST.md) ve [BROWSER-DRIVER.md](BROWSER-DRIVER.md).

## Yeniden çalıştırma

Önce mevcut süreçleri ve açık işleri kontrol et; aynı veritabanına gereksiz ikinci yürütücü başlatma. Komutları `/Users/m/Dev/Verda/v2` içinde çalıştır. Mevcut `.venv` var; yerel veritabanlarını sıfırlama veya eski arşivi tekrar içeri alma.

Chrome açık olmalı. Önceden verilen remote debugging izni yeni oturumda tekrar onay isteyebilir; bu onayı kullanıcı Chrome'da verir. Browser Harness bağlantısı:

```sh
cd /Users/m/Dev/Verda/v2
BU_NAME=verda .venv/bin/browser-harness <<'PY'
print(cdp('Browser.getVersion')['product'])
PY
```

Sonra ayrı terminallerde şu üç süreci çalıştır:

```sh
.venv/bin/verda serve --port 18767
```

```sh
.venv/bin/verda agency-worker --continuous
```

```sh
.venv/bin/verda agency-jev-worker --env-file .local/jev.env --continuous
```

Sunucuyu açmak worker'ları başlatmaz. Jev worker, bağlantı veya anahtar yoksa browser işini sahiplenmeden `waiting_setup` bildirir. Agent kararları için ayrıca yerel Codex CLI ve geçerli oturum gerekir; `codex login status` ile kontrol edilebilir.

Panel erişimi için sunucunun yeni oluşturduğu `.local/viewer-link` dosyasındaki özel tek kullanımlık bağlantıyı yerelde aç. Bağlantıyı Git'e, devir notuna veya herkese açık mesaja koyma. Sunucu yeniden başladığında eski panel oturumu geçersiz olabilir; yeni bağlantı kullan. API anahtarı `.local/api-token` içindedir.

`curl http://127.0.0.1:18767/health` yalnız sunucunun ayakta olduğunu gösterir. Panelde worker, araç, kaynak ve browser durumlarını ayrıca kontrol et. Genel `live_source_adapters` alanı tek başına browser'ın çalışıp çalışmadığını açıklamıyor.

Yeni makinede Python 3.13 ve proje bağımlılıkları gerekir; Jev opsiyonu `pip install -e '.[jev]'` ile kurulur. Yeni boş kurulum ile bu Mac'teki mevcut veriyle devam etme farklı işlemlerdir. Önce özel verilerin taşınmasını ve yedeklerini planla.

## Kod haritası

| Dosya veya dizin | Sorumluluk |
| --- | --- |
| `src/verda/agency/engine.py` | Model kararı, izinli araç çalıştırma, delegasyon ve devam döngüsü. |
| `src/verda/agency/registry.py`, `default.json` | Agent, model ve araç tanımları, yetkiler ve varsayılan promptlar. |
| `src/verda/agency/store.py` | Kalıcı inbox, görev ilişkileri, lease, kaynak kilidi, tetikleyiciler. |
| `src/verda/agency/records.py`, `events.py` | Ortak kayıt, sonuç teslimi, olay ve hata haberleşmesi. |
| `src/verda/agency/research.py` | Arşiv ve güncel kayıtları güvenli inceleme; eksik alan araştırması. |
| `src/verda/agency/settings.py` | Yerel prompt sürümleri ve ayar migrasyonları. |
| `src/verda/agency/browser.py` | Kalıcı browser işleri, motor sahipliği ve doğrulanmış sonuç kabulü. |
| `src/verda/agency/jev.py`, `sahibinden-read.js` | Jev worker, dar karar politikası ve görünür DOM okuyucu. |
| `src/verda/agency/commands.py` | Aktif agency CLI komutları. |
| `src/verda/providers.py`, `runtime.py` | Model sağlayıcısı ve ortak model çalışma parçaları. |
| `src/verda/api.py`, `src/verda/ui/` | Yerel API ve yönetim paneli. |
| `tests/` | Python testleri ve grafik modeli Node testleri. |

Aktif mimari `agency/` altındadır. `workflow.py` ve eski worker/demo parçaları önceki sabit akış denemesidir. README'nin eski kilometre taşı açıklamalarını güncel agency davranışıyla karıştırma.

Önemli ayar ayrımı: `--runtime-config` ve `runtime.example.toml` eski Runtime/manager-demo yoluna aittir. Aktif `agency-worker`, `AgencyConfig` yükler; varsayılanı `agency/default.json`, alternatifi `--agency-config` parametresidir. Agent'ın modeli kendi tanımındadır. Yeni sağlayıcı eklemek yalnız eski Runtime'ı değiştirmekle bitmez; `AgentEngine.providers` kaydı da gerekir. Genel CLI seçenekleri alt komuttan önce gelir:

```sh
.venv/bin/verda --agency-config /tam/yol/agency.json agency-worker --continuous
```

## Yerel veriyi koruma

- `.local/agency.sqlite`: aktif görevler, kanıtlar, teslimler, browser işleri, seçili motor ve yerel prompt sürümleri.
- `.local/verda.sqlite`: içeri alınmış eski ilan/arşiv verisi ve projeksiyonlar.
- `.local/workflows.sqlite`: eski sabit akış demo verisi.
- `.local/jev.env`, `.local/api-token`, `.local/viewer-link`: özel erişim bilgileri.
- `.local/backups`: önceki migrasyon yedekleri.

`.local` Git dışında kalır. Aynı repo başka yerde klonlanınca bu veriler, yerel prompt değişiklikleri ve anahtarlar gelmez. Veritabanı değişikliği öncesinde SQLite'ın tutarlı yedekleme mekanizmasını kullan; çalışan WAL veritabanını yalnız ana dosyayı kopyalayarak yedekleme. Gerçek ilanları, yazışmaları veya kimlik bilgilerini test fixture'ına ve GitHub Pages deposuna koyma.

Yerel prompt editörü araç yetkisini değiştirmez. Yeni prompt sürümü yeni görevlere uygulanır; eski görevin tanımını geriye dönük sessizce değiştirme. Sentetik demo kayıtları ile gerçek `local` kayıtları ayrıdır; test sonucu canlı kaynak kanıtı değildir.

## Devam için önerilen sıra

1. Süreçleri kontrollü başlat, yerel kayıtları yedekle, panel erişimini doğrula. Eski başarısız Jev görevini ve sentetik açık işleri incele; geçmişi koruyarak durumlarını netleştir.
2. Başarılı aramada gerçekten kaydedilmiş tek bir ilan seç. Jev ile ayrıntısını oku; alan, fiyat, ada/parsel, açıklama ve kaynak bağlantısının ortak kayda ve yöneticiye doğru döndüğünü doğrula. Tüm ilanlara birden görev açma.
3. Sahibinden konuşma okuma adaptörünü mevcut yetkili Chrome oturumuna bağla. Eski cevapları, mesaj kimliklerini ve tarihleri koru. Sit sorusunu daha önce cevaplanmışsa tekrar üretmediğini test et.
4. Mesaj gönderim politikasını somutlaştır ve gönderim adaptörünü tamamla. Yinelenmeyi önleyen anahtar, doğrulanmış teslim, belirsiz gönderimi konuşmadan uzlaştırma, hız sınırı ve kullanıcıya dönecek istisnalar birlikte ele alınmalı. Sadece promptta “gönder” yazdığı için canlı gönderimi açma.
5. TKGM'de ada/parsel kimliğini konum ve nitelikle eşleştir. Ardından Mertur giriş noktasını kesinleştir, rota ve rakım araçlarını bağla. Sayısal hesapları statik yap; kaynağı belirsiz bilgiyi doğrulanmış sayma.
6. Yöneticinin eksik kayıtları taramasını sınırlı, yinelenmeyen takip işlerine genişlet. Mevcut inceleme kapsam bütçesini ve dışlanan ilanları koru. Kullanıcı kontrolü olmadan tüm arşivi yeniden sorgulama veya günlük tetikleyiciyi açma.
7. Model/araç/tetikleyici düzenleme ekranlarını ve gerçek görev aktarımı görünürlüğünü tamamla. Yeni agent veya MCP/script eklemek için tanım, adaptör, doğrulama, sonuç kaydı ve arayüz görünürlüğünü birlikte ele al.
8. Yeni kayıtların eski dashboarda aktarımını, süreçlerin otomatik başlatılmasını ve başka model sağlayıcılarını ayrı, ölçülebilir işler olarak tamamla.

İlk somut hedef “tek bir gerçek ilanın ayrıntısını Jev ile okuyup kanıtları kaydetmek ve sonucu yöneticiye döndürmek” olmalı. Çatıyı baştan yazmak veya yalnız yeni görsel maket üretmek bu hedefin yerine geçmez.

## Doğrulama geçmişi

Son kod değişikliğinde 155 Python testi ve 10 Node grafik testi geçti. UI JavaScript sözdizimi ve wheel paketine DOM okuyucunun dahil olması kontrol edildi. Canlı Jev ilk sayfa araması ve sonuçların iki agent görevi boyunca tamamlanması ayrıca doğrulandı. Bu devir belgesi yazılırken kod değiştirilmedi ve tüm test paketi tekrar çalıştırılmadı.

```sh
.venv/bin/python -m pytest -q
node --test tests/graph-model.test.cjs
```

Yeni değişiklikte ilgili testleri çalıştır; canlı kaynağa gereksiz tekrar istek gönderme. Bir paketin kurulu olması, worker'ın bağlı olması, görevin başarıyla bitmesi ve ürün özelliğinin tamamlanması ayrı durumlardır. Durum bildirimlerinde bu ayrımı koru.
