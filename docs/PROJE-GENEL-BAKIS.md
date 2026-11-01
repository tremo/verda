# Verda: genel bakış (yeni bir okuyucu için)

1 Kasım 2026. Bu belge projeyi hiç görmemiş bir geliştirici ya da ajan için yazıldı: ne işe yarar, hangi parça nerede çalışır (kullanıcının Mac'inde mi, uzakta mı), parçalar birbiriyle nasıl konuşur. Ayrıntılar `docs/` altındaki diğer belgelerde; burada yalnız harita var.

## 1. Amaç

Tek kullanıcı, tek konu: **Datça'da tarım amaçlı arazi** (zeytinlik, bademlik, tarla) bulmak. Sahibinden'deki yüzlerce ilanı sistem tarar, her ilanın hangi taşınmaz olduğunu bulur, uygun olmayanları gerekçesiyle eler, kalanları kanıt derecesiyle birlikte sıralar. Karar kullanıcının; sistem öneri ve kanıt üretir.

Ön eleme kuralları (`src/verda/policy.py`): en fazla **15 milyon TL**, en az **1.000 m²**, Palamutbükü'ndeki Mertur Tatil Sitesi'ne araçla en fazla **45 dk**; hisseli satış, 1. derece doğal sit, arkeolojik sit ve yola erişimi olmayan parseller elenir. Elenmemiş ama bilinmeyeni olan ilan "koşullu" ya da "araştırma gerekli" kalır; bilinmeyen asla "olumlu" sayılmaz.

## 2. Büyük resim: ne nerede

```
                         UZAKTA (internet)
 ┌────────────────────────────────────────────────────────────────────┐
 │ Sahibinden ........ ilan ve satıcı yazışması kaynağı (giriş gerekir) │
 │ TKGM (cbsapi) ..... resmî parsel sorgusu: alan, nitelik, geometri    │
 │ OSRM + Valhalla ... iki açık rota motoru (sürüş süresi)             │
 │ ÇŞB sit katmanı ... doğal sit sorgusu                               │
 │ Firestore (proje datca-arsa) .. panonun okuduğu özel veritabanı      │
 │ GitHub Pages ...... pano arayüzü (statik dosyalar)                  │
 │ Firebase Hosting .. datca-arsa.web.app: yalnız GitHub Pages'e yönlendirir │
 │ GitHub ............ iki kod deposu: tremo/verda, tremo/datca-arsa-dashboard │
 │ Codex CLI ......... modelin çalıştığı yer (kimlik bilgisi Codex'te)  │
 └────────────────────────────────────────────────────────────────────┘
                              ▲ ▼
 ┌────────────────────── KULLANICININ MAC'İ (yerel) ───────────────────┐
 │ ~/Dev/Verda/v2 ................ asıl sistem (Python, SQLite)        │
 │ ~/Dev/Verda/current-dashboard . pano arayüzünün yerel kopyası       │
 │ Chrome ........................ kullanıcının kendi oturumu (Sahibinden) │
 └─────────────────────────────────────────────────────────────────────┘
```

**Önemli:** gerçek veri yalnız Mac'te durur (`v2/.local/`, git dışı). GitHub'a yalnız kod gider. Panoya giden veri, kullanıcının Google hesabıyla girilen **özel** Firestore'a yazılır.

### 2.1 Yerelde

| Ne | Nerede | Ne yapar |
|---|---|---|
| Asıl depo | `/Users/m/Dev/Verda/v2` | Python 3.13 paketi `verda` (`src/verda`), testler `tests/` (~40 dosya), belgeler `docs/`, servis tanımları `deploy/launchd/` |
| Ana veritabanı | `v2/.local/agency.sqlite` | Tek gerçek kayıt: ilanlar, gözlemler, iddialar (claims), değerlendirmeler, görevler, mesaj kayıtları, olaylar. **Append-only** tablolar (gözlem, iddia, değerlendirme) tetikleyiciyle değiştirilemez |
| Eski arşiv | `v2/.local/verda.sqlite` | Eski sistemden (OpenClaw dönemi) aktarılan salt okunur anlık görüntü. Eski kontroller, yazışmalar, notlar burada; yol bilgisi ve eski eleme gerekçeleri buradan okunur |
| Günlük yedek | `v2/.local/backups/` | Her gün tutarlı bir kopya, son 7'si saklanır |
| Gizli dosyalar | `v2/.local/` ve `~/Library/Application Support/Verda/secrets/` | API anahtarı, Firestore servis hesabı anahtarı, Jev ayarları. **Hiçbiri depoda değil, okunmamalı** |
| Kayıtlar (log) | `~/Library/Logs/Verda/` | Dört servisin çıktısı |
| Pano kopyası | `/Users/m/Dev/Verda/current-dashboard` | Pano arayüzünün kaynağı; kendi git deposu |
| Eski paket | `/Users/m/Dev/Verda/datca-agentic-app-2026-10-04` ve `review/` | Eski sistemin devir paketi (belgeler, şema, iş kuralları). Tarihsel; v2 bunların yerine geçti |
| Eski tasarım | `/Users/m/Dev/Verda/docs/VERDA-MIMARI.md` | 5 Ekim tasarım önerisi. Hedefi anlatır, bugünkü hâli değil |

### 2.2 Yerelde sürekli çalışan 4 servis (launchd, `deploy/launchd/`)

| Servis | Komut | İş |
|---|---|---|
| `com.verda.panel` | `verda serve --port 18771` | Yerel yönetim paneli ve API: `http://verda.localhost:18771/control`. Yalnız bu Mac'ten erişilir, dış ağa açık değil |
| `com.verda.agency-worker` | `verda agency-worker --continuous` | Görev kuyruğunu işler, agent döngüsünü çalıştırır, zamanlayıcıları tetikler |
| `com.verda.jev-worker` | `verda agency-jev-worker --continuous` | Chrome'u süren yürütücü (Jev / Browser Use): Sahibinden arama, ilan okuma, gelen kutusu |
| `com.verda.publisher` | `verda agency-publish-firestore --every 300` | 5 dakikada bir sonuçları pano biçimine çevirip Firestore'a yazar |

`launchctl list | grep verda` ile görülür. Servisler `caffeinate` ile Mac'i uyutmaz.

### 2.3 Uzakta

| Ne | Adres | Not |
|---|---|---|
| Kod deposu (sistem) | https://github.com/tremo/verda (**public**) | Gerçek ilan, satıcı verisi, anahtar yok; yalnız kod ve belge |
| Kod deposu (pano) | https://github.com/tremo/datca-arsa-dashboard | Pano arayüzü; `main`'e birleşince GitHub Pages'e yayınlanır |
| Pano | https://tremo.github.io/datca-arsa-dashboard/ | Statik HTML/JS. Ilan verisi **içinde değil**; açılınca Google ile giriş yapılır ve Firestore'dan okunur |
| Firebase Hosting | https://datca-arsa.web.app | Yalnız GitHub Pages'e 302 yönlendirir (`current-dashboard/firebase-redirect`) |
| Firestore | Firebase projesi `datca-arsa` | Özel. `v2Listings/*` (ilan başına bir belge), `meta/v2`, `meta/v2status` (durum ve nöbetçi bulguları), `userState/*` (kullanıcının "gördüm" işaretleri vb.) |
| Model | Codex CLI | Model çağrısı gereken adımlar (yönetici kararı, satıcı cevabını yorumlama) bu sağlayıcıyla yapılır. Kimlik doğrulamayı Codex tutar; Verda kimlik bilgisini okumaz |

## 3. Veri nasıl akar

```
Sahibinden ──(Chrome + Jev)──► gözlem ──► İDDİA (claim) ──► doğrulayıcı ──► olgu
TKGM / rota / sit ──(kod)────► gözlem ──►        ▲                              │
satıcı cevabı ──(model okur, kod denetler)───────┘                              ▼
                                                              değerlendirme (evaluate)
                                                                                │
                                              ┌─────────────────────────────────┤
                                              ▼                                 ▼
                                   yerel panel (/control)           yayıncı → Firestore → pano
```

1. **Gözlem:** bir aracın o an gördüğü şey ("ilan sayfası şu fiyatı gösterdi"). Silinmez.
2. **İddia (claim):** gözlemden çıkarılan ya da modelin önerdiği olgu adayı. Her iddianın kaynağı ve (metinse) **birebir alıntısı** vardır.
3. **Doğrulayıcı** (`src/verda/facts/verify.py`, saf kod): kaynak yoksa, alıntı kaynakta geçmiyorsa, araç o alanı üretmeye yetkili değilse reddeder.
4. **Derece:** her olgu dürüst etiketlenir: `kullanici > resmi > bagimsiz > beyan > rapor`. Satıcı beyanı "beyan"dır, resmî sayılmaz.
5. **Birleştirici** (`assemble.py`) olguları toplar, **kural motoru** (`policy.evaluate`) karara çevirir: `excluded`, `outside_search`, `research_required`, `conditional`, `eligible`.
6. **Yayın:** `publish/projection.py` kararı pano biçimine çevirir, `publish/sync.py` yalnız değişen belgeleri Firestore'a yazar.

Eski sistemin kararları (`legacy_excluded`) bazı durumlarda yeni kararı ezer (`facts/service.py`); tek istisna fiyat nedeniyle elenen ilanın fiyatı düşünce yeniden açılmasıdır.

## 4. Agent'lar, görevler, araçlar

Dört agent (`src/verda/agency/default.json`). **Sürekli açık bir model konuşması değil**: kayıtlı bağlamla uyanan yürütücülerdir.

| Agent | Sorumluluk | Başlıca araçlar |
|---|---|---|
| Ana yönetici (`manager`) | Hedefi, boşlukları ve sonuçları izler; işi operatörlere dağıtır | `records.audit`, `facts.status`, `facts.propose`, `listings.rank`, `replies.pending`, `watchdog.check`, `network.reset_modem` |
| Sahibinden operatörü (`sahibinden`) | Sahibinden'de ne gerekiyorsa | `sahibinden.search`, `read_listing`, `list_inbox`, `read_thread`, `send_message`, `listing.photos` |
| Parsel operatörü | Resmî parsel işleri | `tkgm.lookup`, `tkgm.corridor`, `tkgm.neighbors`, `sit.says` |
| Araştırma agent'ı | Hesap ve coğrafya | `geo.route`, `geo.elevation`, `policy.screen`, `listings.rank` |

**Görev** bir agent'a verilen iştir, **araç** işi yapan fonksiyondur, **tetikleyici** (zamanlayıcı, olay) görev üretir. Her adım model gerektirmez: araç çalıştırma, doğrulama, hesap saf kodla yapılır.

Zamanlayıcılar (`src/verda/agency/schedules.json`; çalışma anındaki değerler veritabanındadır): gelen kutusu eşitleme (30 dk), günlük yeni ilan taraması, haftalık tam tarama ve tam gelen kutusu taraması, araştırma süpürmesi (15 dk), satıcı cevaplarını olguya çevirme (30 dk), **nöbetçi** (15 dk).

## 5. Dikkat edilmesi gereken kurallar (bozulursa gerçek zarar olur)

- **Kaynak engeli:** Sahibinden ve TKGM'de 403/429/captcha gelirse kaynak **durur**, hiçbir yolla aşılmaz (başka motor, başka IP, tekrar deneme yok). Devam etmek için kullanıcının onayı gerekir. Sahibinden araç işleri arasında en az 180 saniye vardır; TKGM'nin günlük istek sınırı vardır.
- **Satıcıya mesaj:** tek yazan araç `sahibinden.send_message`. Metni, saati, tıklamayı **kod** belirler; model yalnız ilan numarasını verir. Modlar `off / dry_run / live` (`docs/MESAJ-GONDERME.md`). **Şu an `off`.** Cevaplanmış soru tekrar sorulmaz.
- **Satıcı cevabı anlamla okunur:** cümle kalıbı kuralı yok; model okur, kod yalnız alıntıyı ve kaynağı denetler. "Yola cepheli" = yol var; yalnız doğal sit bahsi arkeolojik sit yok sayılır (kullanıcı kararı).
- **Nöbetçi** (`agency/watchdog.py`, `docs/NOBETCI.md`): her zaman doğru olması gereken kuralları kayıtlardan denetleyen salt okuma kod. Bulgular panoda "Durum" sayfasında görünür. On dakikadan uzun süren "kötü" satırlar (kritik bulgu, engelli kaynak, kapalı servis) Mac'te yerel bildirim olarak da gösterilir (`publish/notify.py`; dışarıya bir şey gitmez, `VERDA_NOTIFY=off` kapatır).
- **Panodaki düzeltmeler geri alınabilir:** "Sistemdeki gibi bırak" ve "Kararı kaldır" Verda'daki kullanıcı iddialarını geri çeker (iddia silinmez, `withdrawn` satırı eklenir; `docs/FACTS-LAYER.md`).
- **Adil fiyat** (`agency/comparables.py`, `opportunity.py`): üç basamak. 1) aynı mahallede yolu bilinen ≥4 benzer parselin ortancası, 2) mahalle katsayılı bölgesel tahmin (hiçbir zaman "güçlü fırsat" demez), 3) veri yetmiyorsa sayı üretmez. Emsaller panoda bağlantılarıyla görünür.
- **Pano akışı:** pano arayüzünde iki katman var. Donmuş eski anlık görüntü + v2'nin Firestore üzerine bindirmesi (`current-dashboard/v2.js`). İkisi birleşerek görünür.
- **Depo public:** gerçek satıcı adı, hesap adı, mesaj metni, anahtar **hiçbir koşulda** repoya girmez.

## 6. Okuma sırası (kod için)

1. `README.md` ve bu belge.
2. `docs/FACTS-LAYER.md`: gözlemden karara giden çekirdek.
3. `src/verda/policy.py`: eleme kuralları (tek yerde).
4. `src/verda/facts/` (`verify.py`, `assemble.py`, `service.py`).
5. `src/verda/agency/engine.py`, `store.py`, `registry.py`: agent döngüsü ve kalıcı kuyruk.
6. `src/verda/agency/research.py`: ilan başına "sıradaki iş ne" mantığı.
7. `src/verda/agency/comparables.py`, `opportunity.py`: adil fiyat.
8. `src/verda/publish/`: Firestore'a yayın.
9. `docs/NOBETCI.md`, `docs/MESAJ-GONDERME.md`, `docs/BROWSER-DRIVER.md`, `docs/JEV-ULTRAFAST.md`.

`docs/CLAUDE-HANDOFF.md` 6 Ekim'e ait eski devir notudur; o günden sonra çok şey değişti (özellikle fakt katmanı, nöbetçi, yayıncı ve launchd servisleri), güncel gibi okunmamalı.

## 7. Bir inceleme yaparken

- Çalıştırma: sadece `cd v2 && .venv/bin/pytest` (veri gerektirmeyen testler). Servisleri başlatma, ağ isteği atma, `.local/` içine bakma.
- Bulgu yazarken: `dosya:satır`, bozulma senaryosu (somut girdiyle), önerilen düzeltme, emin olmadığın yerleri açıkça işaretle.
- Bu sistem tek kullanıcıya ve Mac'e göre tasarlandı; "ölçeklenebilirlik" bulgusu istenmiyor, **doğruluk ve sessiz hata** bulguları isteniyor.
