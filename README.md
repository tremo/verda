# Verda v2

Mac üzerinde çalışan, kalıcı görev kuyrukları olan agent çatısı. Mevcut GitHub Pages arayüzü korunur. Genel agent döngüsü çalışır; canlı emlak araştırması için kaynak adaptörleri ve mesaj politikası henüz tamamlanmamıştır.

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

### Agent merkezi: görev, agent ve araç ayrı kavramlardır

`http://127.0.0.1:8765/control` yeni agent merkezidir. Sunucunun `.local/viewer-link` dosyasına yazdığı tek kullanımlık bağlantıyı önce aynı Mac'in tarayıcısında açın. Bağlantı 10 dakika, salt okunur oturum dört saat geçerlidir. Sunucu yeniden başlatılınca yeni bağlantı gerekir. Bağlantıyı paylaşmayın. Bu oturum kuyruk yazma yetkisi vermez; yazma API'leri Bearer anahtarı ister ve browser Origin başlığını reddeder.

- **Agent:** Sorumluluk alanı, yönergesi, model profili, araç yetkileri, delegasyon hedefleri ve gelen işler kuyruğu olan yürütücü. Sürekli açık bir model konuşması değildir; kayıtlı bağlamla uyanır.
- **Görev:** Bir agent'a atanmış hedef; girdisi, önceliği, ilan ilişkisi, kaynağı ve durumu vardır. “Doğal sit kontrolü” bir görevdir.
- **Araç:** İş yapan fonksiyon, script, kaynak adaptörü veya MCP çağrısıdır. “Doğal sit katmanını sorgula” aracı araştırma agent'ının kullanımına verilebilir.
- **Tetikleyici:** Zaman, kullanıcı isteği, dış olay veya başka agent'ın delegasyonu; görev oluşturur. Kendisi agent değildir.
- **İlan:** Görevlerin üzerinde çalıştığı dosyadır. Aynı ilan birçok agent'a; bir agent aynı anda birçok ilanın sıradaki görevlerine bağlı olabilir.

Yeni merkezde dört başlangıç rolü vardır: ana yönetici, Sahibinden operatörü, parsel operatörü ve araştırma agent'ı. Roller Python enum'uyla sınırlandırılmaz. Yeni bir rol yapılandırmaya eklenebilir. Önceki yedi rolün kataloğu `/control/legacy-definitions`, sabit dokuz görevli örnek ve eski ilan geçmişi `/control/flows` altında tarihsel inceleme için kalır; bunlar yeni çalışma modelini temsil etmez.

Ana görünüm **Akış tuvali**dir. Büyük agent kartlarının içinde araç ikonları bulunur; agent ve zamanlayıcı kendi simgeleriyle ayrılır. Sahibinden operatöründeki **Browser** ikonuna tıklanınca yalnız o agent'ın yetkili olduğu görev türleri açılır: ilan arama, ilan ayrıntısı okuma, mesaj gönderme, yanıt okuma. İşleme tıklanınca gerekli girdiler ve bağlantı durumu görülür. Browser, bu işlemlerin paylaştığı yetkinlik/oturum grubudur; bir ikonun görünmesi oturumun bağlandığı anlamına gelmez. Python, script ve MCP araçları da gruplanır; bu gruplama çalışma yetkilerini genişletmez.

Ana harita zamanlayıcı→agent ve yönetici→operatör bağlarını gösterir. **Ayrıntılı araç ağı** aynı kayıtların bütün agent→araç→kaynak bağlantılarını açar. Bir aracı birden fazla agent kullanıyorsa bu görünümde tek araç düğümüne bağlanırlar. Agent düğümüne tıklanınca kalıcı prompt, model, araçlar, tetikleyiciler, görev devri yetkileri ve gelen işler sağ panelde açılır.

Zamanlayıcı düğümü kayıtlı saati (Europe/Istanbul), tekrar aralığını, etkin/pasif durumunu, hedef agent'a gönderilen görev metnini, girdiyi, önceliği ve oluşmuş görevleri gösterir. Sabit tekrar aralığı çalışma süresi değildir: mevcut çekirdekte toplam görev süresi sınırı yoktur; agent'ın karar turu ve tek model çağrısı sınırları ayrıca gösterilir. Doğrudan statik araç işi oluşturan tetikleyicide model çağrısı olmadığı belirtilir. Olay tetikleyicisi olay türünü ve gerçek girdi kaynağını gösterir.

**Çalışma izi** seçeneği aynı tuvalde seçilen izin görevlerini, gerçekleşmiş devirleri, araç çağrılarını ve kaydedilmiş sonuç dönüşlerini çizer. Görev ve çağrı düğümleri verilen girdiyi ve kaydedilmiş çıktıyı açar. Tanımlı bir yetki gerçekleşmiş işlem olarak gösterilmez. Yakınlaştırma, sığdırma, tuvali ve düğümleri sürükleme vardır; konumlar yalnız mevcut sayfa oturumundadır. Bu teslimat inceleme tuvalidir: bağlantı kurma/silme, prompt veya zamanlama değiştirme ve görev başlatma henüz yoktur.

Araç kartı yerel Python kodunu, scripti, MCP kaydını ve henüz bağlanmamış adaptörü ayırır; yapılandırılmış bir script/MCP bağlantısı doğrulanmış gibi gösterilmez. Browser oturumunun durumu, paylaşılan kaynak, istek aralığı ve yazma politikası da görünür. Eski agent listesi ve kayıt sekmeleri ayrıntılı liste görünümü olarak korunur.

Agent'ın gerçek yönergesi, model tercihi, delegasyon hedefleri, tetikleyicileri ve ilan bazında görev izi ayrı sekmelerde incelenebilir. Kuyruk bekleyen/çalışan ve tamamlanan işleri ayırır; her işin kaynağı, önceliği, girdisi, sonucu ve araç çağrıları görünür. Doğrudan statik araçla yürütülen görev “Model kullanmaz” olarak işaretlenir. Toplam sayaçlar bütün görevleri kapsar; listede son 200 görev, olaylarda son 1.000 kayıt sınırı ve kesilme uyarısı vardır.

Üst durum kartları panelin açık olmasıyla görev yürütücüsünün çalışmasını ayırır. Worker bildirimi işlenirken en fazla 600 saniye, boşta 15 saniye geçerlidir; normal kapanışta durdu olarak kaydedilir. Bu gösterge işletim sistemi süreç denetimi değildir; ani kapanış son bildirim süresi dolana kadar görünmeyebilir. “Yenile” kayıtları tekrar okur; bu sayfa worker başlatmaz. Varsayılan kayıtta bir çalışır Python hesaplaması vardır; bağımsız script ve MCP aracı henüz eklenmemiştir.

### Mimari değerlendirme: merkezi yönetim ve veri sahipliği

**Önerilen sonraki adım; aşağıdaki ortak bulgu deposu ve otomatik olay teslimi henüz uygulanmadı.** Mevcut `agency_events` araç sonuçlarını ve görev geçmişini saklar; bu, alan bazında kanıt/provenans içeren ortak ilan bilgi deposunun tamamlandığı anlamına gelmez.

Ana yönetici araştırmanın hedefini, önceliklerini, yeni görevlerini ve ilan hakkındaki kararlarını yönetmelidir. Her ham bulguyu ana yöneticinin modeline yeniden yazdırmak maliyet, gecikme ve tek noktada tıkanma yaratır. Operatörler de ortak ilan satırını serbestçe değiştirmemelidir. Yazma işlemini yetki ve şema denetleyen **tek kayıt servisi** yapmalıdır; ilk Mac sürümünde bu ayrı bir sunucu değil, aynı uygulama içindeki normal bir Python modülü olabilir.

| Kayıt | Kim üretir? | Nasıl kaydedilir? |
|---|---|---|
| Kaynak gözlemi: ilan metni, fiyat, koordinat, yanıt | Kaynağa erişen operatör/araç | Kayıt servisi, kaynak zamanı ve kanıt referansıyla değişmez bir gözlem ekler |
| Normalleştirilmiş alan: TL, m², koordinat sistemi | Statik dönüştürücü/doğrulayıcı | Kaynak gözlemine bağlı, sürümlü alan kaydı; çelişki eski veriyi ezmez |
| Karar: ek inceleme, adaylık, sonraki görev | Ana yönetici + bağlayıcı politika kontrolleri | Kanıt kimliklerine dayanan ayrı karar kaydı |
| Dış işlem: satıcı mesajı, dashboard yayını | Yetkili yürütücü | Mesaj politikası, tekilleştirme ve teslimat kaydıyla kontrollü çıkış |

İlan ayrıntısı okuma akışı: operatör aracı çağırır → çalışma motoru sonucu kayıt servisine verir → gözlem ve `observation.recorded` teslim kaydı aynı veritabanı işleminde oluşur → olay ana yönetici kuyruğuna gelir → yönetici kayıt kimliklerini ve kısa özeti okuyup sonraki işi seçer. Yönetici çalışmıyorsa olay bekler; gözlem kaybolmaz. Olay yeniden teslim edilirse aynı görev iki kez oluşturulmaz. Ham kanıtın tamamını her model çağrısına kopyalamak gerekmez.

Zamanlayıcı, önceden tanımlanmış rutin bir işi doğrudan operatöre verebilir; her sabah aynı aramayı başlatmak için yöneticiye model çağrısı yaptırmak gerekmez. Böyle bir görevin sonucunun da yöneticiyi uyandırması gerekir. **Mevcut eksik:** `_wake_parent` yalnız devredilmiş alt görevin üst görevini uyandırır. Zamanlayıcının doğrudan oluşturduğu, üst görevi olmayan işin sonucu otomatik olarak genel olay aboneliğine teslim edilmez. `publish` ile dış olay teslimi vardır; tamamlanmış görevden bu teslimata dayanıklı bağ henüz yoktur.

Diğer sınırlar: “araştırma agent'ı” karar/değerlendirme rolüyle sınırlandırılmalı; rota, rakım ve eşik kontrolü gibi belirli hesaplar statik araçlarda kalmalıdır. Browser bağlantısı ile onun sunduğu `search/read/send` işlemleri ayrıdır; tek Browser yetkisi sınırsız gezinme veya mesaj gönderme izni olmamalıdır. Mevcut `effect=write` kapısı bütün yazmaları mesaj politikası olmadığı için durdurur; gelecekte yerel gözlem kaydı, dış mesaj ve yayın yetkileri ayrı etkiler ve kurallarla tanımlanmalıdır. Yerel bulgu kaydı bir satıcı mesajıyla aynı onay yoluna sokulmamalıdır.

## Ürün hedefi: n8n tarzı görsel otomasyon stüdyosu

Kullanıcının 5 Ekim 2026 yönlendirmesiyle ürünün ana yüzeyi **görsel akış editörü** olarak tanımlandı. Tuvalde düğümler eklenir, bağlanır ve ayarlanır; aynı tuval bir çalışma seçildiğinde yürütme izini gösterir. n8n burada etkileşim ve bileşen modeli için referanstır; n8n'i kurma veya mevcut uygulamayı ona taşıma kararı verilmiş değildir. Referans: [n8n kavramları](https://docs.n8n.io/key-concept-glossary.md), [akış oluşturma](https://docs.n8n.io/build-your-first-workflow.md).

**Bu bölüm hedef tasarımdır.** Mevcut agent merkezi bağlantı haritası ve çalışma izi gösteren bir inceleme tuvalidir. Aşağıdaki akış tanımlarını düzenleme/yayımlama, akış tanımı çalıştırıcısı ve düğüm ekleme/bağlama işlemleri henüz uygulanmadı. Agent kuyrukları, model döngüsü, araç kayıtları ve olay geçmişi bu katmanın altında kullanılacak mevcut çekirdektir.

### Görsel bileşenler

| Düğüm türü | İşlevi | Model gerekir mi? |
|---|---|---|
| Tetikleyici | Zaman, kullanıcı, webhook veya olayla akış başlatır | Hayır |
| Agent'a görev ver | Kayıtlı agent'ın kuyruğuna hedef ve girdi gönderir; sonucu bekleyebilir veya görev kimliğini hemen döndürebilir | Göreve göre |
| Araç / MCP | Yetkili bir aracı açık girdilerle çağırır | Hayır |
| Script / hesap | Kayıtlı kodu çalıştırır | Hayır |
| Koşul / filtre | Veriyi kurallarla dallara ayırır | Hayır |
| Her kayıt için / birleştir | İlanları tek tek işler, paralel sonuçları toplar | Hayır |
| Bekle / kullanıcı girdisi | Yanıt, süre veya insan kararı gelene kadar kalıcı olarak duraklar | Hayır |
| Alt akış | Yeniden kullanılabilir bir akışı çağırır | İçeriğine göre |
| Kaydet / bildir | Doğrulanmış sonucu veri katmanına yazar veya tanımlı bildirimi üretir | Hayır |

Agent kutusu yeni bir agent kopyası oluşturmaz; kayıtlı agent kimliğine referanstır. Sabah taraması, ilan detayı toplama ve yönetici mesajları üç ayrı akış olsa da hepsi aynı Sahibinden operatörünün kuyruğuna bağlanır. Aynı tarayıcı ve hesap için hız sınırı bütün akışlarda ortaktır. Araç, akıştan doğrudan çağrılsa da aynı yetki ve kaynak kontrollerinden geçer.

Akış bağlantısı **veri/çalışma sırasını**, agent'ın araç bağlantısı ise **kullanma yetkisini** ifade eder. Editör bunları farklı gösterir. Bir aracı agent'a bağlamak her görevde otomatik çağrılacağı anlamına gelmez. Agent'ın çalışma sırasında seçtiği araç ve delegasyonlar yürütme görünümünde açılır; tasarım tuvaline sabit adımlar gibi eklenmez.

### İki görünüm, aynı akış

- **Tasarım:** Solda düğüm kataloğu, ortada tuval, sağda seçili düğümün ayarları. Agent düğümünde seçilen agent, hedef, girdi eşlemesi ve bekleme davranışı bulunur. Agent'ın ortak prompt/model/araç ayarlarına ayrıca gidilir.
- **Çalıştırma:** Seçilen çalışmanın gerçek düğüm durumları, giriş/çıkış verileri, süreler, hata ve bekleme nedenleri aynı tuvale bindirilir. Agent adımı açılınca kararlar, araç çağrıları ve alt görevler görünür.

Bir ilan birden çok akıştan geçmiş olabilir. İlan dosyası hepsini tek zaman çizelgesinde toplar; buradan ilgili akış çalışmasına ve düğüme geçilir. Tasarım ile çalışmış geçmiş birbirine karıştırılmaz.

### Kalıcı akış sözleşmesi

Görsel editörün kaynak kaydı sürümlü bir `FlowDefinition` olacak: düğümler, düğüm türü ve sürümü, ayarlar, tipli giriş/çıkışlar, veri eşlemeleri, bağlantılar ve tuval konumları. UI çizimi bu kaydı düzenleyecek. Bağlantı bilgilerinin kendisi tanımda tutulmaz; yetkili yerel bağlantı kayıtlarına referans verilir.

Her çalıştırma bir `FlowRun` oluşturur ve yayımlanmış tanım sürümünü sabitler. Düğüm denemeleri `NodeRun` olarak kaydedilir. Agent düğümü mevcut gelen işler kuyruğunda bir görev açar; `flow_run_id`, `node_run_id`, `task_id`, `tool_call_id` ve `listing_ref` birlikte izlenir. Sonuç, ilgili bekleyen düğümü uyandırır. Worker veya tarayıcı oturumu bekleme boyunca açık tutulmak zorunda değildir.

Taslak düzenleme çalışan akışı değiştirmez. Test çalışması ile etkin zamanlanmış çalışma ayrı tutulur. Yerel kaydedilmiş örnek çıktı üzerinden düğüm testi yapılabilmesi, Sahibinden'e geliştirme sırasında gereksiz tekrar isteklerini azaltır. Tek düğümü yeniden çalıştırma, mesaj gibi dış etkileri körlemesine tekrarlamaz; kayıtlı teslimat durumunu kontrol eder.

### İlk uçtan uca editör kabulü

İlk dikey parça: **Elle başlat → agent'a görev ver → sonucu kaydet** akışını tuvalden oluştur, kaydet, yeniden aç ve sentetik araçlarla gerçek model döngüsünde çalıştır. Aynı agent'ı ikinci akıştan çağırıp ortak kuyruğu göster. Bir düğüm seçildiğinde o çalışmanın gerçek girdisini, araç çağrısını ve sonucunu göster. Sonraki düğüm türleri bu kayıt/çalıştırma sözleşmesine eklenir; her yeni düğüm için uygulama baştan yazılmaz.

Mevcut GitHub Pages ilan dashboard'ı korunur. Bu editör ayrı yerel yönetim yüzeyidir. Datça araştırması onun ilk uygulamasıdır; agent ve araç ekleyebilme temel ürün özelliğidir.

## Agent çatısının mimarisi

```mermaid
flowchart LR
  U[Kullanıcı] --> I[Kalıcı görev girişi]
  S[Zamanlayıcı] --> I
  E[Dış olay / yanıt] --> I
  I --> Q[Agent gelen işler kuyruğu]
  Q --> A[Bağlamı oku ve karar ver]
  A --> T[Yetkili araç çağrısı]
  A --> D[Başka agent'a görev ver]
  A --> W[Girdi bekle veya tamamla]
  D --> I
  T --> R[Sonucu ve işlem izini kaydet]
  R --> Q
  W --> P[Üst göreve sonucu bildir]
  P --> Q
```

**Sahibinden örneği:** Sabah zamanlayıcısı tarama işini operatörün kuyruğuna verir. Yeni bulunan ilanlar için detay okuma işleri aynı operatöre gelir. Yönetici de farklı ilanlara ait konuşma veya mesaj görevleri atayabilir. Kuyruk öncelik, ardından geliş sırasını kullanır. Agent başına bir karar turu, kaynak başına bir araç çağrısı aynı anda çalışabilir. Bir kaynak beklemesi agent'ın diğer uygun işlerine geçebilmesine izin verir. Kritik bir gönderim başladıktan sonra başka bir iş için ortasında kesilmez.

Yönetici sabit DAG oluşturmak zorunda değildir. Görev bağlamı, önceki araç sonuçları ve dönen alt görevlerle karar verir: araç kullan, devret, kullanıcı girdisi bekle veya tamamla. Yönetici `delegate` ile sonucu bekleyebilir veya `dispatch` ile beklemeden birden fazla görevi kuyruğa bırakabilir. Tamamlanmamış alt işler varken üst görev tamamlanamaz. Alt işler tamamlandığında üst görev tekrar kuyruğa alınır; alt görevde engel oluşursa yönetici yeniden karar vermek için uyandırılır. Bekleyen üst görev worker veya model oturumu tutmaz. Bir görev başka agent'a devredilirken girdi kopyası, üst/alt görev bağı ve ortak iz kimliği saklanır. Bir iz en fazla 100 görev; her agent görevi yapılandırılmış tur bütçesiyle sınırlıdır.

Görevlerin sonucu doğrudan resmî kanıt veya uygunluk kararı sayılmaz. Alan uygulamasında belge/konum doğrulaması ve kanıt kabulü ayrıca araçlarla uygulanmalıdır. Genel çatı araştırma sırasını bilmez; bu kurallar görev yönergeleri, araç sözleşmeleri ve alan doğrulayıcılarına aittir.

### Çalışan çekirdek

Yeni durum `.local/agency.sqlite` dosyasındadır. Eski arşiv ve önceki workflow veritabanları aynen korunur. `agency-init` tekrar çalıştırılabilir; şema v1→v2→v3 yükseltmesi işlemseldir. v3 görev yürütücüsü durum bildirimlerini ekler. Yedeklerde üç veritabanı da bulunmalıdır.

- Kalıcı gelen işler kuyruğu, öncelik, atomik sahiplenme, agent başına tek karar turu.
- Agent bazında sağlayıcı/model, sürümlü yönerge ve araç/delegasyon izinleri. Görev oluşurken tanımın kopyası ve yapılandırma özeti saklanır; çalışırken yapılandırma değişmişse sessizce farklı yetkiyle devam etmez.
- Codex ile gerçek karar döngüsü, dinamik görev devri, alt görev sonucuyla yöneticinin yeniden uyanması.
- Python, sabit komutlu script ve MCP araç kaydı. MCP stdio bağlantısı yerel test sunucusuyla doğrulandı; HTTP transport kod yolu mevcut, dış servis kimlik doğrulaması henüz eklenmedi.
- Statik görevlerde `executor_tool` doğrudan belirtilir; agent'ın kuyruğu ve araç izinleri kullanılır, **model çağrısı yapılmaz**.
- Kaynak kilidi ve araç çağrıları arasında kalıcı minimum süre. Kaynak durdurma sinyali alan adaptör `ResourceBlocked` üretir; bağlı kuyruk durur. Gerçek tarayıcıdan CAPTCHA/429/403/oturum kaybı tespiti henüz bağlı değildir.
- Zamanlayıcı teslimatı ve dış olay aboneliği; tekrar gelen istekler aynı anahtarla çoğaltılmaz. Kaçırılmış zaman aralıkları tek teslimata birleştirilir.
- Girdi bekleyen göreve cevap gelince devam etme. Araç çağrısı başlamışken worker kaybolursa belirsiz durum saklanır, kaynak durur; otomatik tekrar yapılmaz.
- İşlem açıklaması, araç girdisi/çıktısı, görev devri, model sağlayıcısı, çağrı kimliği, kullanım ve prompt sürümü olaylara yazılır. Bunlar modelin iç düşünce kaydı değildir.

### Başlatma ve görev verme

```bash
.venv/bin/verda agency-init
.venv/bin/verda agency-submit sahibinden "Atanan ilanın ayrıntısını oku" \
  --inputs .local/listing-input.json --listing-ref LISTING_ID --request-key read-LISTING_ID-v1

# Kuyruktan tek karar turu. Araç bağlı değilse açık nedenle bekler.
.venv/bin/verda agency-worker --max-steps 1

# Sürekli yerel worker ve zamanlayıcı teslimatı; terminalde Ctrl+C ile durdurulur.
.venv/bin/verda agency-worker --continuous

# Gerçek Codex; yalnız sentetik araçlar ve örnek ilan. Sabit görev planı yok.
.venv/bin/verda agency-demo
.venv/bin/verda agency-status
```

`agency-demo`, yöneticiye yalnız hedef verir. Yönetici operatöre devretmeyi, operatör araç çağrısını ve tamamlamayı, yönetici de sonucu nasıl özetleyeceğini modelle seçer. Sentetik araçlar ayrı kaynak adları kullanır ve canlı işleri tamamlamaz. 5 Ekim 2026 yerel doğrulamasında yönetici → Sahibinden operatörü → örnek ilan okuma aracı → operatör sonucu → yönetici sonucu zinciri **dört gerçek Codex çağrısıyla** tamamlandı. Bu Sahibinden bağlantısının çalıştığı anlamına gelmez.

Salt kodla çalışan görev örneği (`.local/screen.json` içeriği `{"price_tl": 8000000, "area_m2": 2000}`):

```bash
.venv/bin/verda agency-submit research "Fiyat ve alan ön elemesi" \
  --inputs .local/screen.json --tool policy.screen --request-key screening-example
.venv/bin/verda agency-worker --max-steps 2
```

API'den görev girişi `POST /api/agency/tasks`, olay girişi `POST /api/agency/events`, kullanıcı cevabı `POST /api/agency/tasks/{id}/reply` yollarındadır. İlk ikisi tekrar üretmeyi önlemek için istek anahtarı alır. Başka bir uygulama veya MCP sunucusu bu yetkili girişleri çağırabilir; Verda'nın kendi MCP **sunucusu** henüz yoktur. Verda'nın MCP **istemcisi** araç tüketmek için uygulanmıştır.

### Yeni agent, script veya MCP aracı ekleme

Başlangıç tanımları `src/verda/agency/default.json` dosyasındadır. Yerel kopyayı düzenleyip `--agency-config .local/agency.json` parametresini komuttan önce verin. Araç eklemek onu bütün agent'lara açmaz; ilgili agent'ın `tools` listesine de eklemek gerekir. Modelin yazdığı komut veya sunucu adresi çalıştırılmaz; bunlar yalnız güvenilen yerel yapılandırmadan gelir.

Bir agent kaydı `key`, `label`, `description`, `prompt`, `version`, `model`, `tools`, `delegates`, `max_turns` içerir. Model profili `provider`, `model`, `timeout_seconds` alanlarıdır. Yeni sağlayıcı aynı `ModelProvider.generate` arayüzüyle engine'e kaydedilir; başka firma adını yazmak adaptörü kendiliğinden kurmaz. Şu anda Codex uygulanmıştır.

Script araç kaydı örneği:

```json
{
  "key": "local.measure",
  "label": "Yerel ölçüm",
  "description": "Kayıtlı ölçümü hesaplar",
  "transport": "script",
  "command": ["/absolute/path/to/python", "/absolute/path/to/measure.py"],
  "input_schema": {"type": "object", "properties": {"value": {"type": "number"}}, "required": ["value"], "additionalProperties": false}
}
```

Script JSON girdisini stdin'den okur, JSON çıktısını stdout'a yazar. Shell açılmaz. Script sandbox'ı yoktur; yapılandırılan script güvenilen yerel kod olmalıdır. Python fonksiyonları `Registry(config, handlers={"tool.key": callable})` ile bağlanır.

MCP araç kaydında `transport: "mcp"`, `remote_name`, `server_url` **veya** stdio `command` tanımlanır. Girdi şeması ve agent yetkisi yerelde açıkça tanımlanır. Sunucunun sunduğu diğer araçlar veya yönergeler kendiliğinden içeri alınmaz. SDK: [resmî MCP istemci belgeleri](https://py.sdk.modelcontextprotocol.io/client/).

Zamanlayıcı JSON kaydı `key`, `kind: "timer"`, `next_at` (UTC Unix zamanı), `spec: {agent, objective, inputs, interval_seconds, priority}` içerir. Günlük kullanım için başlangıç zamanı açıkça hesaplanır ve aralık 86400 saniyedir; saat dilimli genel cron takvimi henüz yoktur. Olay kaydı `kind: "event"`, `spec.event_type` ve aynı görev hedefini içerir. Kayıt için `agency-trigger FILE`, dış olay teslimi için `agency-event EVENT_TYPE FILE --request-key KEY` kullanılır. Zamanlayıcı yalnız worker çalışırken teslim edilir; Mac kapalıyken bulut yürütme yoktur.

### Açık kalan alan işleri

Bu teslimat genel agent çekirdeğini değiştirir; canlı emlak operasyonunu tamamlamaz. Seçilmiş Chrome oturumu, Sahibinden/TKGM bağlantıları, gerçek kanıt kabulü, ortak ilan bilgi deposuna yeni gözlem yazan araçlar, mesaj kurallarının uygulanması ve teslimat uzlaştırması, genel cron/saat dilimi desteği, kontrol panelinden düzenleme/başlatma ve canlı Firestore yayını ayrı tamamlanacak parçalardır. Worker otomatik sistem servisi olarak kurulmaz. Yazma etkili araçlar mesaj politikası olmadığı sürece çalıştırılmaz. Belirsiz çağrıların yeniden denenmesi ve konfigürasyon değişmiş görevlerin taşınması için otomatik uzlaştırma yoktur.

Sahibinden için 180 saniye **araç çağrısı** aralığı tanımlıdır; bir araç birden çok site isteği yapıyorsa alt tarayıcı katmanı da bunları sınırlamalıdır. Bu sitenin izin verdiği veya engellemeyeceği bir sınır değildir. Önceki 5 işlik grup/30 dakika mola önerisi henüz uygulanmamıştır.

### Eski ilan geçmişi ve sabit plan

`/control/flows` eski ilanların korunmuş olaylarını ve önceki sabit görev örneklerini sunar. Eksik agent/prompt/girdi kaydı geriye dönük üretilmez. Gerçek ilana demo bağlanmaz. Bu ekranla yeni agent merkezinin görev kayıtları ayrı tutulur. Mevcut GitHub Pages dashboard'ı değiştirilmez.

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

## Önceki sabit görev motoru (korunan örnek)

Eski verinin arşivi `.local/verda.sqlite`, yeni görev kuyruğu `.local/workflows.sqlite` içinde tutulur. `verda init` iki veritabanını kurar; aynı komut tekrar çalıştırılabilir. Eski görevler otomatik olarak yeni kuyruğa aktarılmaz. İki veritabanını aynı dosyaya yönlendirmeyin; şema kontrolü bunun kabul edilmesini engeller. Yedekte her iki dosya da yer almalıdır.

Standart araştırma sırası:

```text
İlanı oku → parsel kimliğini çöz → TKGM kimlik/geometri doğrulaması
  → doğal sit / arkeolojik sit / erişim / rota / rakım
  → değerlendirme
```

Bu eski örneğin sırasını kurallı planlayıcı oluşturur. Yeni agent çekirdeği bu planı kullanmaz; yukarıda açıklanan dinamik karar/delegasyon döngüsünü kullanır. Plan dışı yetkiler, döngüsel bağımlılıklar ve parsel adımı olmadan mekânsal kontrol isteyen planlar reddedilir. Gerçek araştırmanın ucuz ön eleme, eksik kimlik sorusu, yanıt bekleme ve kanıt kabul dalları bu sabit örnek plana henüz eklenmedi.

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

## Önceki tek çağrılık model katmanı

Bu bölüm korunan `runtime-info` ve `manager-demo` komutlarını açıklar. Yeni `agency-*` komutlarının model, prompt ve araç ayarları yukarıdaki JSON agent tanımlarındadır. İki katman aynı Codex sağlayıcı arayüzünü kullanır.

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
node --test tests/graph-model.test.cjs
```

Testler sentetik SQLite, geçici dosyalar ve yerel HTTP istemcisi kullanır. Kaynak değişmezliği, tekrar aktarım, geçmişin ve mesaj durumlarının korunması, yetkisiz erişim, politika eşikleri ve dashboard alanlarının kaybolmaması kontrol edilir. Görev motorunda eşzamanlı sahiplenme, süre aşımı, yeniden başlama, deneme sınırı, bağımlılıklar, iptal ve örnek/gerçek iş ayrımı ayrıca test edilir.

Yeni agent döngüsü, dinamik delegasyon, kalıcı gelen işler, model kullanmayan görevler, kaynak sıralaması, olay/zamanlayıcı girişi ve script/MCP araçları ayrıca test edilir. Canlı kaynak adaptörleri, kanıt kabulü, mesaj politikası ve otomatik Firestore yayını henüz tamamlanmamıştır.
