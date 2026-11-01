# Nöbetçi: her zaman doğru olması gereken kurallar

14 Ekim 2026'da cevaplanmış bir soru satıcıya yeniden gitti; bunu sistem değil kullanıcı fark etti. Aynı gün yapılan taramada aylardır süren başka sessiz hatalar da çıktı: bir haftadır işlenmemiş satıcı cevapları (içinde 1. derece sit yanıtları), tek tapuya bakılıp elenen iki tapulu ilan, bir ilana günde 33 gönderim denemesi, hiç bitmeyen haftalık tarama. Hepsinin ortak noktası: bozulduklarında kimse uyarmadı.

Nöbetçi (`agency/watchdog.py`) bu kuralları kayıtlardan kontrol eder. **Salt okur; hiçbir şeyi düzeltmez ve karar vermez.** Bozulan kural, arkasındaki ilanlar ve sayılarla bir bulgu olur.

## Nasıl çalışır

- **Araç** `watchdog.check` (yönetici agent'ının yetkisinde, Python, salt okuma).
- **Zamanlayıcı** `watchdog` (`schedules.json`): 15 dakikada bir yöneticinin kuyruğuna statik görev bırakır; araç **model çağrısı olmadan** çalışır. Sonuç ortak kayıt servisine `calculation` gözlemi olarak yazılır, yani geçmiş kontrol edilebilir. Yöneticinin kendi görevi olduğundan kimseyi uyandırmaz.
- **Pano yayıncısı** aynı kontrolü her yayında ayrıca çalıştırır. Bulgular panonun Durum sayfasında (`meta/v2status.health` ve `watchdog`) kırmızı/sarı satır olarak görünür. Böylece ajan işçisi kapalıyken de bozulan kural görünür.
- **Panel** dikkat listesinde (`attention.py`) her bulgu bir konudur. Kritik olanlar "KRİTİK ·" ile başlar. İlanlı bulgu aynı ilanlar için bir kez, ilansız bulgu günde bir kez "gördüm" ile kapatılabilir.
- Kontrol edilemeyen kural geçmiş sayılmaz: "Nöbetçi kuralı kontrol edilemedi" bulgusu olur.

## Kurallar

| Kural | Ne zaman bozulur | Önem |
|---|---|---|
| `repeat_question` | Son 7 günde giden bir mesaj, o anki konuşmada sorulmuş ya da satıcının kendiliğinden yazdığı bir konuyu sordu; ya da konuşma "bulunamadı" iken gitti | kritik |
| `send_policy` | Canlı mesaj 08:30–21:00 dışında ya da bir öncekine `sender.gap_minutes`'tan yakın gitti | kritik |
| `send_loop` | Bir ilana 24 saatte 4+ gönderim denemesi (tıklamadan duranlar dahil) | eylem |
| `replies_waiting` | Bir satıcı cevabı 24 saattir ne olguya çevrildi ne işaretlendi | eylem |
| `conversation_lost` | Daha önce okunmuş konuşma son okumada "bulunamadı" (aktif ilanlar). İş kuyruğu bunu mesaj gönderimi açık olsun olmasın günde bir kez yeniden okutur (1 Kasım'a kadar yalnız soru gidecekken okutuyordu; gönderim kapalıyken dört konuşma 18 gün bekledi) | eylem |
| `exclusion_doubt` | "En az alan" elemesi, ilanın kendi alanından %20+ farklı resmî alana dayanıyor | eylem |
| `inbox_list_short` | Son 24 saatte "tam" işaretli bir gelen kutusu listesi, en uzununun %90'ından kısa | uyarı |
| `step_loop` | Bir tarayıcı/resmî kaynak adımı aynı ilan ya da parsel için 24 saatte 8+ kez | eylem |
| `stuck_work` | Bir adım aynı ilan için bugün `queue.stuck_after` kez başarısız oldu ya da bir gelen kutusu satırı iki kez; sistem bunları yarına bırakır, bu kural bırakıldıklarını gösterir. Sonradan başarılı olan sayılmaz | eylem |
| `tool_failures` | Bir araç 24 saatte 10+ çağrının %20'sinden fazlasında başarısız | uyarı |
| `scan_coverage` | 36 saattir arama sayfası okunmadı; haftalık tam tarama 8 gündür yok ya da son sayfaya ulaşmadı | uyarı |
| `stale_listings` | Aktif ilan 8 gündür ne aramada ne ilan sayfasında görüldü | uyarı |
| `publisher_silent` | Pano 20 dakikadır yazılmadı | uyarı |
| `worker_silent` | Bekleyen iş varken ajan işçisi 30 dakikadır hareketsiz | uyarı |

Eşikler `watchdog.py`'nin başındadır. Kuralların her biri bir olaydan doğdu (`tests/test_watchdog.py` o olayı yeniden kurar). 14 Ekim kayıtları üzerinde geriye dönük çalıştırıldığında yanlış giden mesajı 18:44'te, 32 denemelik döngüyü 13 Ekim akşamı, kaybolan 15 konuşmayı 15:16'da bulur.

## Kural eklemek

1. Değişmezi tek cümleyle yaz (fonksiyonun belgesi o cümledir).
2. `fn(store, mode, now) -> [bulgu]` yaz; yalnız oku, kayıtlara dayan, `_finding` ile döndür, en çok 25 ilan.
3. `RULES` listesine ekle; olayı yeniden kuran ve sağlıklı durumda susan iki test yaz.
4. Sonucu 32 KB kayıt sınırının altında tut (bütün rapor bugün ~5 KB).

## Sınırlar

- Bildirim telefona itilmez; Durum sayfası ve panel gösterir. On dakikadan uzun süren kötü satırlar bu Mac'te yerel bildirim olur (`publish/notify.py`, `VERDA_NOTIFY=off` kapatır). Mac kapalıyken kontrol de yayın da durur.
- Nöbetçi olan bir şeyi görür; gelecekteki bir hatayı önlemez. Önleme kapılardadır (`MESAJ-GONDERME.md`).
