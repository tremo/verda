# Satıcıya mesaj gönderme

8 Ekim 2026. Tek yazan araç: `sahibinden.send_message`. Model yalnız ilan numarasını verir; **metni, hangi alanların sorulacağını, saati ve tıklamayı kod belirler.** Görünür Chrome'da, Sahibinden'in kendi mesaj formundan gider; doğrudan mesaj API'si kullanılmaz.

## Modlar (Ayarlar > Mesaj gönderme)

| Mod | Ne yapar |
|---|---|
| Kapalı | Hiçbir şey yazılmaz. Soru bekleyen ilanlar `waits_for_sender` sayılır. |
| **Deneme (varsayılan)** | İlan canlı okunur, hesap doğrulanır, mesaj formu açılır, metin yazılır ve **birebir okunur**, "Gönder" düğmesinin etkin olduğu görülür, taslak silinir. **Göndermez.** En çok `sender.dry_run_limit` (3) taslak; sonra incelemen beklenir. |
| Canlı | Aynı adımlar; sonra tıklama niyeti kalıcı yazılır, **bir kez** Gönder'e basılır, konuşma okunur. Yalnız kullanıcı seçer; seçerken ayrıca onay sorulur. |

## Kapılar (`message.plan`, saf kod)

Sırayla: mod · saat 08:30–21:00 İstanbul (yalnız canlı) · çalışma başına tek mesaj · iki gerçek mesaj arası `sender.gap_minutes` (3) · ilan aday olmalı (elenmemiş, yayında, eski sistem de elememiş) · kural motorunun sıradaki adımı `ask:*` olmalı · önceki mesajın teslimi belirsizse yazılmaz · son mesaj bizimse (satıcı cevap vermedi) tekrar yazılmaz · sorulacak alan kalmalı · aynı mesaj daha önce gittiyse yazılmaz.

Tek mesajda hepsi sorulur: ada/parsel, kadastral yol, doğal sit, arkeolojik sit. Her alanın önkoşulu ayrıca aranır: konuşma okunmuş olmalı; yol, resmî geometri kontrolünden sonra. Satıcı cevabı net değilse mesaj yazılmaz, ilan "netleştirme gerekli" olur (`listing.flag`).

Metin sabit şablondur (`message.render`): "Merhaba, {ilan no} numaralı taşınmaz için … Teşekkürler." Telefon, yapay zekâ/asistan, arama talebi, karavan, WhatsApp içeremez (`check_text`).

## Güvenlik sırası (`send.MessageSender`)

1. Canlı ilan açılır: kimlik, yayında mı, başlıkta görünen hesap (yerel ayardaki hesap, `.local/account.json`), **ilan metni okunduğundan beri aynı mı** (özet). Değişmişse gitmez, önce yeniden okunur.
2. `banaozel.sahibinden.com/mesajlarim/detay/{ilan}/yeni` açılır; adres, hesap, KVKK penceresi, "ilan yayında değil" / "mesaj almamayı tercih ediyor", telefon uyarısı denetlenir. Yalnız Sahibinden'in **standart karşılıklı telefon paylaşımı** uyarısı kabul edilir (kullanıcı kararı); başka telefon ifadesi → durur.
3. Metin yazılır, `textarea[placeholder="Mesajınızı yazınız"]` değeri **birebir** okunur. Uymazsa taslak silinir, gönderilmez.
4. Deneme: taslak silinir, biter. Canlı: `BrowserBridge.checkpoint_send` tıklama niyetini **tıklamadan önce** yazar (sahip + metin özeti eşleşmeli); reddederse tıklanmaz.
5. `button.send-button[title="Gönder"]` bir kez tıklanır. Konuşma adresi + metnin son kullanıcı mesajı olarak aynen görülmesi → `confirmed`; aksi → `delivery_unknown`.

## Aynı şeyi iki kez sormama: üç bağımsız kontrol (14 Ekim 2026)

Plan tek bir konuşma okumasına bakar; o okuma yanılırsa (konuşmayı bulamazsa) plan da yanılır. 14 Ekim'de takılan bir gelen kutusu okuması kendini "tam liste" sandı, 15 konuşma "yok" sayıldı ve 1214768369'a satıcının 20 Eylül'de cevapladığı yol sorusu yeniden gitti. O günden beri gönderim üç ayrı yoldan kontrol edilir; biri bile "dur" derse tıklanmaz:

1. **Kapı** (`message.history_refusal`, `evaluate` içinde): ilanın **bütün** konuşma okumaları, bu sistemin ve eski sistemin gönderdiği bütün mesajlar okunur. Sorulacak alan daha önce sorulduysa ya da satıcı o konuda kendiliğinden yazdıysa → `asked_before` (yanıtı kullanıcı değerlendirir). Konuşma bir kez okunmuş ama son okuma bulamamışsa → `conversation_unconfirmed`; kuyruk konuşmayı yeniden okutur, soru o zamana kadar gitmez.
2. **Tıklama öncesi** (`BrowserBridge.checkpoint_send`): aynı geçmiş kayıtlardan yeniden okunur; kapının kararına güvenilmez. Reddederse tıklanmaz, makbuz nedeni taşır.
3. **Canlı sayfa** (`send.py`): Sahibinden, konuşması olan ilanda formu konuşmanın içinde açar. Ekrandaki mesajlar konuyu kapsıyorsa → `asked_before_in_thread`.

Kapsam ölçüsü `learning.covers` (metindeki sözcükler): soru metni sabit şablon olduğundan bizim mesajlarımızda kesindir; satıcının serbest metninde "yol", "sit", "parsel" geçmesi yeterli sayılır (yanlış pozitif → soru gitmez, kullanıcı bakar; tersi hiç olmamalı). Geçmişteki 45 gönderimin hepsiyle denendi: yalnız yanlış giden 1214768369'u durdurur.

Döngüler: tıklamadan duran denemeler de deneme sayılır (`queue._sends_stopped`); "ilan değişti" ile duran ve sayfası yeniden okunamayan ilana gönderim denenmez (`listing_changed_unreadable`).

## Giden kutusu (`outbox`)

`prepared → sending → confirmed | delivery_unknown | dry_run | cancelled`. `sending` + `click_intent` aşamasındaki satır **hiçbir zaman kendiliğinden yeniden denenmez**. Tıklamadan önce biten hata satırı `prepared`'a döndürür, çağrı sıradan başarısızlık sayılır; tıklamadan sonraki kayıp/engel `delivery_unknown` + `uncertain` çağrıdır. `delivery_unknown`, sonraki konuşma okumasında metin aynen görülürse kendiliğinden `confirmed` olur; olmazsa kullanıcı ilan sayfasında "Gitti / Gitmedi" der.

## Dosyalar

`message.py` (şablon, kapılar, giden kutusu) · `send.py` (tarayıcı adımları, `CdpPage`) · `browser.py` (`SendReceipt`, `checkpoint_send`, bitiş/kayıp kuralları) · `engine.py` (`_send`) · `jev.py` (`send_job`) · `queue.py` (tek tek görev) · `watchdog.py` (gönderimden sonra da aynı kuralları denetler) · `learning.py` / `attention.py` / `ui/listing.js` (görünürlük) · testler: `test_message`, `test_send`, `test_sender`, `test_watchdog`.

## Doğrulanan ve doğrulanmayan

Canlıda (8 Ekim 2026): ilan okuma, hesap, form, standart telefon uyarısı, birebir yazma, düğme durumu, silme ve yönetici → operatör → tarayıcı → makbuz hattı **deneme modunda** uçtan uca çalıştı. **Gerçek gönderim (tıklama + konuşma doğrulaması) canlıda henüz denenmedi**; yalnız sahte sayfayla test edildi.
