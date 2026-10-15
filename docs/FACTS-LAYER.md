# Olgu katmanı: gözlem → doğrulanmış olgu → karar

7 Ekim 2026. Kod: `src/verda/facts/`. Şema v8 (`claims`, `assessments`). Hiçbir modül model çağırmaz.

## Neden var

Araçlar *gözlem* döndürür ("ilan sayfası şunu gösterdi"). Kural motoru (`verda.policy.evaluate`) ise *doğrulanmış olgu* ister. Arada iki iş vardı ve ikisi de eksikti: metinden/gözlemden olgu çıkarmak ve olgunun kabulünü kodla doğrulamak. Ürünün amacı çok sayıda arsayı taramaktır; tarama aşamasında satıcı/ilan beyanı yeter, resmî doğrulamayı alma aşamasında kullanıcı yapar. Bu yüzden katman iki şeyi garanti eder:

1. Her "olgu"nun arkasında gerçekten kayıtlı bir kaynak ve (metin ise) birebir alıntı vardır.
2. Her olgunun **derecesi** dürüsttür: beyan beyan olarak, resmî kayıt resmî olarak etiketlenir. Kullanıcının alma öncesi doğrulama listesi buradan çıkar.

Yanlış *eleme* görünmez ve pahalıdır; yanlış "geçti" ucuzdur (alma aşamasında resmî doğrulama zaten yapılacak). Doğrulayıcı bu asimetriye göre ayarlıdır.

## Akış

```
gözlem (read_listing · read_thread · tkgm · rota)        ← mevcut, değişmez
        │  aynı işlemde (store._finish_tool)
        ▼
   İDDİA ◄── statik çıkarıcılar (kod) · LLM önerisi · kullanıcı kararı
        │
        ▼
   DOĞRULAYICI (verify.py, saf kod) ──► ret / inceleme (needs_review)
        │ kabul
        ▼
   claims (append-only)  →  birleştirici (assemble.py)  →  ScreeningFacts  →  evaluate()
                                                                                  │
                                                          assessments (yalnız değişince) ◄┘
```

Roller: **LLM okur ve önerir, kod kabul eder, kullanıcı istisnayı çözer.** Serbest metin (ilan açıklaması, satıcı mesajı) regex'le değil modelle okunur; kod yalnız denetler. Çıkarıcılar (`extract.py`) sadece yapısal alanları okur: fiyat, alan, ada/parsel, "Tapu Durumu" satırı.

## Doğrulayıcı (`verify.py`)

Bir iddia şunların hepsi geçerse olgu olur:

| Kontrol | Başarısızlıkta |
|---|---|
| Alan ve değer biçimi geçerli (`0/0` yer tutucu, bool≠sayı, sözlük dışı enum) | ret |
| Gözlem var, **aynı mod** (sentetik gerçek olgu üretemez) | ret |
| Araç bu alanı üretmeye yetkili (`FIELDS[...].tools`) | ret |
| İlan kimliği **gözlemin içeriğinden** doğrulanır, etiketten değil | ret |
| Model yalnız metin taşıyan araçlardan öneri yapar; sayıları kod çıkarır | ret |
| Alıntı zorunlu; kaynakta **birebir** geçer (Türkçe `İ/ı` katlamalı) | ret |
| Konuşmada mesaj kimliği verilmeli; mesajı **satıcı** yazmış olmalı | ret |
| Alıntı `…`/`...` içermez; kaynak mesaj kesik değil | ret |
| Doğal/arkeolojik sit türü alıntıyla uyuşur | ret |
| `absence` dayanağı: yalnız kod, yalnız `shared_title=False`, yalnız tam ilan okumasından, alıntısız | aksi halde ret; kabul = `rapor` |
| Alıntı çok kısa (<6 karakter) | `needs_review` |
| Alıntının **cümlesi** çekingen ("sanırım", "araştırılmalı"…) | `needs_review` |
| Eleme doğuran öneri (hisseli, arkeolojik, 1. derece, erişim yok) ve konu "bu parsel" değil | `needs_review` |

`needs_review` kullanıcıya alıntıyla gider: `agency-facts-resolve CLAIM_ID --accept|--reject --note ...`. Kabul, kaynağın **kendi derecesini** korur; beyanı resmî kayda çevirmez.

## Kanıt dereceleri (`policy.py`, sürümlü veri)

Güçlüden zayıfa: `kullanici` · `resmi` · `bagimsiz` · `beyan` · `rapor`. Derece kaynağı izler (araç → derece, `model.TOOL_GRADE`), öneren kişiyi değil. `Policy.field_grades` her alan için hangi derecelerin **tarama için yeterli** olduğunu söyler (eski `policy.json > fallback_evidence_policy` ile aynı ayrım): sit ve erişim için beyan yeter; rota, satış kapsamı için yetmez. Yetmeyen kayıt "ipucu" (`leads`) olarak durur, `evaluate`'e girmez.

- **"İlan hisseli demiyorsa hisseli değildir."** Tam ilan okumasında `Tapu Durumu` satırı yoksa veya tanınmayan bir değerse ve başlıkta/açıklamada/özniteliklerde hiç hisse sözcüğü (`hisse`, `paylı`, `müşterek`, `ortak tapu`) geçmiyorsa kod `shared_title=False` varsayar. Bu varsayım `rapor` derecesindedir: açık "Müstakil Parsel" satırı (`beyan`) ve sonradan gelen herhangi bir açık hisse beyanı onu ezer; alma öncesi listede (`to_verify_officially`) görünür. Hisse sözcüğü geçiyorsa kod varsaymaz, okumayı modele/kullanıcıya bırakır (sözcük listesi yalnız bu varsayımın kapısıdır, hiçbir şeye karar vermez).
- Aynı kaynak ve derecenin yeni kaydı eskisini geçersiz kılar (fiyat değişimi çelişki değil sürümdür). Aynı gözlemin iki bölümü ayrışırsa (başlık satırı ≠ açıklama) bu sürüm değil çelişkidir.
- Aynı derecede **farklı kaynaklar** ayrışırsa çelişkidir: alan askıya alınır (`conflicts`), kullanıcıya gider.
- **Resmî kayıt ilanı düzeltir.** Resmî/bağımsız kayıt ile ilan veya satıcı beyanı ayrışırsa bu çelişki değildir: resmî değer geçerlidir, ilandaki/beyandaki yanlış `listing_errors` olarak kaydedilir (alan, nitelik, sit…). TKGM ile ilan alanı birkaç m² farklıysa (1.053 ≈ 1053,41) bu da hata sayılmaz. Çelişki yalnız **kullanıcı kararı** ile resmî sonuç arasında kalır: kullanıcı kararı çalışma varsayımıdır ama resmî sonuç gizlenmez.
- Resmî parsel bulununca satış kapsamı çözülmüş sayılır (parsel ada/parselle adlandırılmıştır); ilandaki alan farklıysa resmî alan geçerlidir ve sert eleme onunla çalışır.
- Kart verisi (`rapor`) geçicidir; ayrıntı sayfasından farklı olması çelişki sayılmaz.
- Kimlik (ada/parsel) çelişkisi yalnız **parsele bağlı** olguları (`scope=parcel`) askıya alır; fiyat ve ilanın kendi beyanları etkilenmez.

`evaluate` çelişki yüzünden erken dönmez: kalan olgularla sert eleme çalışır; çelişki varsa sonuç `research_required` kalır ve neden `conflict:<konu>` olarak yazılır.

## Veri

`claims` ve `assessments` append-only (tetikleyiciyle). Claim: ilan, mod, alan, değer, kapsam, kaynak türü, gözlem kimliği, kaynak referansı, alıntı, öneren (`code:…@n` · `llm:<agent>` · `user`), durum, derece, neden, doğrulayıcı sürümü. Aynı öneri tekrar gelirse çoğalmaz (`dedupe_key`); kullanıcı kararları olay sayıldığı için A→B→A korunur. `assessments` yalnız olgular, dereceler veya politika sürümü değişince yeni satır alır (kanıt kimlikleri parmak izine girmez).

## Okuma modeli

`facts.status(listing_id)` aracı ve `verda agency-facts-status ID`: birleşik olgular + dereceler, kural motoru kararı, ipuçları, çelişkiler, inceleme bekleyenler, `next_action`, `to_verify_officially` (hâlâ yalnız beyana/rapora/kullanıcı varsayımına dayanan, resmî olarak doğrulanabilir alanlar = alma öncesi kontrol listesi). Eski sistemde elenmiş ilan her zaman `none/legacy_excluded` döner. `ask:*` aksiyonları mesaj niyetidir; gönderim için ayrı uygunluk kapısı gerekir (henüz yok).

## Komutlar

```sh
.venv/bin/verda agency-facts-backfill                     # v8 öncesi gözlemlerden olgu çıkar (idempotent)
.venv/bin/verda agency-facts-status LISTING_ID
.venv/bin/verda agency-facts-decide LISTING_ID sale_scope_resolved true --note "Tek parsel, TKGM alanı uyuşuyor"
.venv/bin/verda agency-facts-resolve CLAIM_ID --accept --note "Satıcıyla teyit"
```

**Yükseltme:** herhangi bir `agency-*` komutu veya `serve` şemayı otomatik v7→v8 yapar. Önce yedek alın (`agency-before-facts-*` örneği `.local/backups/` altında). v8 veritabanını eski kodla açmayın.

## Genişletme

- **Yeni resmî araç** (TKGM, rota…): `model.FIELDS[...].tools` ve `TOOL_GRADE`'e ekleyin, bir çıkarıcı yazın (`extract.py`), `EXTRACTORS`'a ekleyin. Derece araçtan gelir.
- **`read_thread`:** yük sözleşmesi `{"listing_id", "messages": [{"id", "role": "seller"|"user", "text", "truncated"?}]}` (`verify.source_texts`). Bu biçimi döndürmeyen okuyucu doğrulayıcıdan geçemez, bilerek.
- **Yeni alan:** `FIELDS`, `Policy.field_grades` ve (`evaluate` girdisiyse) `ScreeningFacts`/`FactService.screening`.

## Resmî parsel, rota, fotoğraflar, ilan dosyası (7 Ekim)

- **`tkgm.lookup`** (`parcel` agent'ı; `agency/tkgm.py`): ada/parsel + mahalle adıyla resmî alan, nitelik, geometri ve sorgu bağlantısı. Mahalle adı birebir eşleşmeli, dönen parsel istenen olmalı, 403/429 kaynağı durdurur. Alan Türk biçimindedir (`1.053,41`).
- **`geo.route`** (`research` agent'ı; `agency/geo.py`): parselin **TKGM merkezinden** (satıcının işaretlediği nokta değil) Mertur'a OSRM + Valhalla. Hedef `Policy.destination` içinde (kullanıcı verdi, 7 Ekim). Başlangıçta yol yoksa en yakın yola oturtup yeniden dener ve `fallback: nearest_road` yazar; hiç yol yoksa uydurmaz. İki motor aynı OSM verisini kullanır, trafik yoktur.
- **`listing.photos`** (`sahibinden` agent'ı; `agency/photos.py`): okuyucunun topladığı foto adreslerini yalnız sitenin CDN'inden (`*.shbdn.com`, `*.sahibinden.com`, https) indirir; bayt doğrulaması (JPEG/PNG/WebP), 6 MB sınırı, içerik-adresli özel depo (`agency.sqlite.evidence/`, 0600, git dışı), şema v9 `evidence_assets`.
- **İlan dosyası:** `/control/listing/<ilan no>` (viewer bağlantısıyla): karar, olgular ve dayanakları, resmî parsel, rota, ilan açıklaması, fotoğraflar. Fotoğraflar `/control/private/assets/<sha256>` ile yalnız viewer oturumuna sunulur.
- Foto seçicileri gerçek sayfada henüz sınanmadı: ilk canlı okumada kontrol edilip düzeltilecek.

## Satış kapsamı, ada 0, satıcı cevapları (14 Ekim)

- **Satış kapsamı** (`assemble._check_scope`): TKGM parseli bulduğunda "satılan bu parsel" der. Resmî alan ilandaki alandan %20'den fazla farklıysa bu söz geçmez: iki tapulu ilan (1252932673: 2582 + 678 m², yalnız 678 sorgulandı), yanlış parsel numarası (1336424878: 4540 m² ilan, 222 m² parsel) ya da hisse (1216510607: 1318 / 2606 m²). O zaman `sale_scope` çelişkisi doğar: en az alan kuralı çalışmaz, ilan elenmez, `human_review` olur; fırsat puanı m² fiyatını kullanmaz ("Belirsiz") ve emsal verisine girmez. %20'nin altındaki farklar ölçüm güncellemesidir; resmî alan geçerlidir.
- **Ada 0**: adası olmayan parsel TKGM'de boş ada ile döner ve `/parsel/<mahalle>/0/<parsel>` ile sorgulanır (Karaköy 0/1238 → 1.463,18 m², 14 Ekim'de denendi). `PARCEL`, `split_parcel`, araç şeması ve yol koridoru (`corridor.identity`: boş ada = 0) bunu kabul eder; 0/0 yer tutucu olarak reddedilir.
- **Satıcı cevapları** (`replies.pending`): her zaman konuşmayı **bulan** son okumaya bakar (sonraki "bulunamadı" cevabı gizlemez); ada/parsel de yorumlanır; A1–A3 arkeolojik, D1–D3 doğal sit dereceleridir. Yönetici net bulmadığı cevabı `listing.flag` ile işaretler, sessizce atlamaz; işaretli alan yeniden yoruma verilmez.
- **Gözlem kimliği**: araç çıktısı model bağlamına `observation_id` ile girer (`store._finish_tool`); okuduğunu öneren ajan kaynak kimliğini bilir.

## Birden çok tapu, kullanıcı kararları (15 Ekim)

- **`sale_parcels`**: satış birden çok parselse hepsi (ana parsel önce). Kaynak: kullanıcı ya da ilan/satıcı beyanı (alıntıyla). Her ek parsel `tkgm.lookup {part: true}` ile sorgulanır ve yalnız kendi alanını (`parcel_area_m2`, parsele göre) yazar; satış alanı parsellerin resmî alanları toplamıdır. Bir parsel sorgulanmadan alan açık kalır, eleme yapılmaz; sıradaki adım o parselin sorgusudur.
- **Parsele bağlı kayıtlar** (`service.with_parcels`): TKGM, resmî sit, koridor ve komşu kayıtları hangi parsel içinse o parsele bağlanır; satışın parselleri dışındaki parsele ait kayıt ilanı etkilemez. Kullanıcı yanlış parsel numarasını düzeltince (1336424878: 894/3 bir yol parseliydi) eski parselin alanı ve sit sonucu kendiliğinden devre dışı kalır, yeni parsel sorgulanır.
- **Resmî sit** satışın bütün parsellerine bakar, en kısıtlayıcı sonuç geçerlidir; parseller değişince yeniden çalışır (`says.stale`). **Yol koridoru** ana parselde çalışır; çok parselli satışta "kenarlar komşu parsel" sonucu kesin sayılmaz (yol kardeş parselden olabilir).
- **`user_excluded`** (kullanıcının "ele" kararı, panodaki "İlgilenmiyorum" dahil): kesin eleme, ilan için başka iş yapılmaz. **`scope_question`**: alan uyuşmazlığında kullanıcı "satıcıya sor" derse sabit şablonla "ilanda X m², tapu kaydında Y m²; tamamı mı, bir kısmı mı, hisse mi?" sorulur; cevap hisse ise `shared_title`, başka parseller ise `sale_parcels` önerilir, belirsizse işaretlenir.

## Henüz yok

- LLM'in öneri aracı (`facts.propose`): doğrulayıcı hazır ve testli, araç ve yönerge bağlantısı sıradaki dilim.
- İnceleme kuyruğu için panel ekranı (şimdilik CLI).
- Araç sınırı kapıları (`listing_ref` ↔ `listing_id` çapraz kontrolü, eski sistemde elenmişe okuma görevi açmama): `facts.status` bilgiyi verir, motor henüz zorlamaz.
- TKGM, rota, sit kesişimi, konuşma okuma, mesaj uygunluğu ve gönderim.
- Dashboard'a (Firestore) yayın; `listingFeedback` geri alma kanalı.
