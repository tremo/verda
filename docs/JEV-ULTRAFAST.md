# Browser Use / Jev Ultrafast

Verda'nın isteğe bağlı ikinci tarayıcı motoru [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast), `1231850a0bf1a0c0341fe408ef1668dbbfdfac46` commit'ine sabitlenmiştir. Browser Harness bağımlılığı `0.1.13` sürümüdür. `pip install -e '.[jev]'` bu sürümleri kurar. Yönetici ve operatör modelleri Codex olarak kalır; Jev'in tarayıcı kararları ayrı TypeSafe sağlayıcısını kullanır.

## Bağlantı

1. Chrome açık olmalı. Chrome'un `chrome://inspect/#remote-debugging` ekranındaki izin kullanıcı tarafından açılmalı. Bu tarayıcı düzeyinde kontrol iznidir. Chrome bağlantı sırasında ayrıca Allow/İzin ver penceresi gösterebilir. Verda güvenlik ayarını kendisi değiştirmez, çerez veya profil kopyalamaz.
2. Yerel Browser Harness oturum adı `verda` olmalıdır. Yetkili bağlantıyı terminalden başlat:

   ```sh
   BU_NAME=verda .venv/bin/browser-harness <<'PY'
   print(cdp('Browser.getVersion')['product'])
   PY
   ```

3. Anahtarı Git dışındaki yerel bir dosyada tut (ör. `.local/jev.env`, izin `0600`):

   ```dotenv
   TYPESAFE_API_KEY=your-key
   TYPESAFE_MODEL=jev-latest
   ```

4. Panelde **Tarayıcı bağlantısı → Browser Use · Jev Ultrafast → Motor seçimini kaydet**. CLI karşılığı: `verda agency-browser select --driver jev`. Bekleyen/çalışan tarayıcı işi varken motor değiştirilemez; eski işler kendiliğinden başka motora aktarılmaz.
5. Model yürütücüsü yanında tarayıcı yürütücüsünü çalıştır:

   ```sh
   .venv/bin/verda agency-jev-worker --env-file .local/jev.env --continuous
   ```

`--continuous` olmadan en fazla bir hazır tarayıcı işi yürütülür. Bu süreç aktif Codex görüşmesine ihtiyaç duymaz; Mac, Chrome, Browser Harness ve iki Verda yürütücüsü açık olmalıdır. Oturum açılışında otomatik başlatma/yeniden başlatma servisi bu değişiklikle kurulmaz.

Yürütücü anahtar veya Chrome bağlantısı eksikse işi sahiplenmeden `waiting_setup` döndürür. Panel son kontrolün nedenini ve tarihini gösterir. Paket kurulu, motor seçili, yürütücü bağlı ve görev başarılı farklı durumlardır. Başlatma kontrolü API anahtarının varlığını kontrol eder; sağlayıcı tarafından kabul edildiği ilk model çağrısında anlaşılır. Anahtar dosyasındaki yalnız izinli model alanları yüklenir; shell çalıştırılmaz, değerler panel/API/loglara dönmez. `TEXT_MODEL_*` gelecekte metin girişi için yüklenebilir; mevcut okuma kapsamında kullanılmaz.

## Bu sürümün kapsamı

- `sahibinden.search`: Datça satılık arsa / verilen fiyat-alan filtreleriyle **ilk sayfa**. URL kurma ve sonuçları çıkarma statik koddur.
- `sahibinden.read_listing`: daha önce ortak kaynak kaydında görülen ilan URL'sini açar. Model URL veya selector üretmez.
- Jev'in gerçek `choose` karar fonksiyonu yalnız `SCROLL_UP`, `SCROLL_DOWN`, `WAIT`, `DONE`, `BLOCKED` seçeneklerini görür. Kaynakta gezinme ilk sabit URL ile sınırlıdır. Serbest tıklama, metin yazma, hesap girişi, sayfalama ve mesaj gönderme açık değildir.
- Her iş en fazla 12 karar ve döngü başında kontrol edilen 240 saniye bütçe taşır; kaynak işleri arasında mevcut en az 180 saniye kuralı korunur. İş içindeki kaydırma/bekleme en az 2 saniye aralıklıdır. Bir kararın sağlayıcı çağrısı zaman aşımı bu bütçeye ek gecikme getirebilir; kuyruk sahipliği 600 saniyede kesin olarak dolar.
- `DONE` tek başına başarı değildir. Bağımsız DOM okuyucu görünür satırları, filtreli URL'yi, ilan kimliğini, sayı/fiyat/alan tutarlılığını doğrular. Gizli satırları ve boş sonuç toplamını uydurmaz. Kaynak DOM'u değişirse başarı yerine hata üretir.
- CAPTCHA, erişim/hız engeli ve oturum kaybı aynı ortak kaynak olayına gider. Engelde otomatik tekrar veya başka motorla kaçış yoktur. Yarım kalmış sahiplenme önceki belirsiz işlem kuralıyla durur.
- Jev adımları görev akışında `browser_progress` olaylarıdır. Sonuç `browser_driver: jev_ultrafast` ile ortak kayda yazılır; yöneticiye mevcut teslim mekanizmasıyla döner. Sayfanın ekran görüntüleri, ham model istekleri ve anahtarlar olaylara yazılmaz.

TypeSafe'a yalnız bu iş için açılan herkese açık ilan sayfasının görünür metni ve görev kapsamı gönderilir. Diğer Chrome sekmeleri, çerezler ve özel satıcı yazışmaları model girdisi değildir. Bu kapsamın ileride mesajlaşmaya genişletilmesi ayrıca araç ve gönderim politikası gerektirir.

## Doğrulama

`pytest tests/test_jev.py tests/test_management.py` paket/sürücü ayrımı, anahtar yokken kuyruğa dokunmama, eski işler için motorun korunması, kaynak sonucu → ortak kayıt → görev tamamlama, modelin hatalı DONE kararı, CAPTCHA ve yasak tıklamaları test eder. Ağ ve canlı API kullanmaz. Canlı Jev doğrulaması için kullanıcının TypeSafe dosyası ve izinli Chrome bağlantısı gerekir; testlerin geçmesi canlı kaynağın bağlandığı anlamına gelmez.

5 Ekim 2026'da canlı Chrome ve TypeSafe bağlantısıyla yönetici → Sahibinden operatörü → Jev → ortak kayıt → yönetici akışı doğrulandı. Filtreli Datça aramasında ilk sayfadaki 20 ilan kaydedildi; kaynak toplam 295 sonuç bildirdi. İki agent görevi de tamamlandı. Site filtre parametrelerini yeniden sıraladığı için ilk deneme durdu; URL kontrolü sıralama yerine aynı sayfa ve filtre değerlerini karşılaştıracak şekilde düzeltildi, ardından akış başarıyla çalıştı. Gerçek kayıtlar ve anahtarlar yalnız `.local` altında tutulur.
