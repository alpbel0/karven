# Kaynak Profili: Türkiye İstatistik Kurumu (TÜİK)

- Araştırma klasörü: `KARVEN-ARAŞTIRMA\tuik\`
- Araştırma dönemi: 07.09.2026 – 14.09.2026
- Profil tarihi: 13.09.2026 (güncelleme: 14.09.2026 — sınıflama sunucusu,
  gerçek SDMX-CSV/JSON-stat body yakalamaları eklendi, bkz. §1.8 ve §3)
- Profili dolduran: Claude (Codex CLI `gpt-5.6-luna`/xhigh ile koordineli araştırma oturumu)
- Durum: KARVEN reposuna taşındı (RULES.md §11 onayı alınmış, §3'teki tüm
  fixture'lar `tests/fixtures/tuik/` altında)

> Bu dosya `TEKNIK-TASARIM-2026-09-04.md` §4.1–§5.1'in ürettiği tek özet
> dosyadır. `KARVEN-ARAŞTIRMA\tuik\` altındaki TÜM ham araştırmayı (69+
> kanıt dosyası, `tuik/evidence/`, `tuik/medas/`, `tuik/TODO-yeni-kesifler.md`)
> özetler. Doğrulanmamış hiçbir teknik iddia buraya girmez; her iddia gerçek
> bir istek/yanıt örneğiyle desteklenir (RULES.md §1, §4, §5). Kapsam yalnızca
> TÜİK'tir — TCMB kullanıcı talebiyle kapsam dışı bırakılmıştır, bu profilde
> yer almaz.
>
> **Kapsam netliği:** Bu profil, araştırmada fiilen erişilebildiği doğrulanan
> tüm TÜİK veri sistemlerini, endpoint ailelerini, dataflow kümelerini ve veri
> formatlarını kapsar. Aynı teknik şemayı kullanan dataflow'lar için ortak
> alanlar bir kez açıklanır; 421+ dataflow'un tam envanteri ve erişim kanıtları
> araştırma klasöründe korunur. Erişilemeyen veya test edilmeyen alanlar
> destekleniyor kabul edilmez ve açıkça listelenir.

---

## 0. Yönetici özeti

TÜİK, verilerini **tek bir birleşik API üzerinden değil, en az 8-9 ayrı
teknik sistem/portal ailesi** üzerinden sunuyor; bunların çoğu birbirinden
bağımsız geliştirilmiş, farklı kimlik doğrulama modelleri ve farklı
güvenilirlik profillerine sahip. En önemli, üretim-hazır bulgular:

1. **`nsiws.tuik.gov.tr` SDMX REST API**, TÜİK'in resmi/dokümante edilmiş
   makro-istatistik servisidir (TÜFE, GSYH, işsizlik vb. ~421+ dataflow).
   Bearer token (Keycloak, ~5 dk ömürlü) gerektirir ve **sabit, öngörülebilir
   ~%25 sessiz-timeout oranına** sahiptir (429 değil — sunucu tarafı
   yük-dengeleme/havuz doygunluğu paterni, bkz. §1.3). Kanıt seviyesi A.
2. **`databrowser2.tuik.gov.tr` JSON-stat API**, nsiws'in resmi olmayan ama
   **token gerektirmeyen** bir alternatifidir; aynı dataflow katalogunun
   büyük kısmında (110/114 örneklemde) çalışır ve XML/CSV/JSON export
   endpoint'leri de token'sızdır. Üretim connector'ı için **birincil aday**.
   Kanıt seviyesi A.
3. **`www.tuik.gov.tr/Turcat`** — IMF SDDS Ulusal Veri Sayfası: 258 satırlık,
   token'sız, WAF'sız, tek GET'lik, IMF DSBB'ye doğrudan bağlı, en güncel +
   önceki dönem karşılaştırmalı temiz JSON. Geçmiş seri yok, ama "hızlı özet
   gösterge" ihtiyacı için TÜİK'te bulunan en yüksek kaliteli tek kaynak.
   Kanıt seviyesi A.
4. **Veri Portalı API'leri** (`veriportali.tuik.gov.tr/api/tr/*`: basın
   bültenleri, toplu indirme kataloğu ve dosyaları) **düz HTTP ile**
   çalışıyor; tarayıcı gerekmiyor. WAF yalnızca başlık kontrolü yapıyor:
   Chrome `User-Agent` + `X-Requested-With: XMLHttpRequest` (02.10.2026
   ölçümü, §1.5). Kanıt seviyesi A.
5. **MEDAS** (bölgesel/il düzeyi istatistikler, ZK Framework tabanlı) 92/92
   konu tam tarandı; **CİP GetMapData proxy** (token'sız, il/ilçe düzeyi
   regional data) `duzey` 1-4 çalışıyor, 5 hiç veri döndürmüyor.

**Bulunan ayrı sistem/alt-portal sayısı: 9** — nsiws SDMX REST, databrowser2,
MEDAS (+CİP proxy), Turcat/Ulusal Veri Sayfası, basın bülteni API'si, Yayın
Sistemi (632 kayıt tam taranmış), Veri Portalı (arama/kategori), Seçim
İstatistikleri (1950'ye kadar), bi.tuik.gov.tr (Qlik BI, dış ticaret),
turizmapp (ZK, turizm).

**Connector için önerilen öncelik sırası:**
1. `databrowser2` JSON-stat/CSV/XML export (token'sız, geniş kapsam)
2. `Turcat` (hızlı özet/dashboard göstergeleri için tamamlayıcı)
3. Basın bülteni API'si (güncel bülten metinleri/PDF referansları için)
4. `nsiws` SDMX REST (yalnızca databrowser2'de bulunamayan alanlar için,
   token yönetimi + retry mantığıyla)
5. MEDAS/CİP (bölgesel kırılım gerektiğinde)

## 1. Kaynak teknik profili

### 1.1 nsiws SDMX REST API (resmi, dokümante edilmiş)

```text
İddia: TÜİK'in resmi SDMX 2.1 REST veri servisi; TÜFE, GSYH ve 421+ diğer
       dataflow'u kapsıyor.
Kaynak sayfa: https://veriportali.tuik.gov.tr/tr/sdmx-web-service-documentation
Araştırma tarihi: 07-09.09.2026
Browser işlemi: Doğrudan URL açma + PowerShell script ile toplu istek
İstek yöntemi: GET
İstek URL'si: https://nsiws.tuik.gov.tr/rest/data/TR,DF_TUFE_SDMX_TT10,1.0/TR.M.TUFE.1._Z.2025.2026_01._Z.0.F_TFE
Query parametreleri: startPeriod, endPeriod (opsiyonel — ikisi de atlanabilir,
  bu durumda tam geçmiş seri döner)
Request body: (yok, GET)
Gerekli header/cookie: Authorization: Bearer {token}
HTTP durum kodu: 200 (başarılı) / 401 (token yok) / 404 (bulunamayan seri) /
  422 (semantik hata, örn. geçersiz tarih formatı) / sessiz timeout (~%25)
Response content-type: application/vnd.sdmx.genericdata+xml
Response içindeki ilgili alan: <generic:Series>/<generic:Obs>, OBS_VALUE
Gerçek örnek değer: TÜFE endeks/değişim serisi, 2005-01'den başlıyor
Tekrar üretilebilirlik: Yüksek ama istatistiksel — aynı istek 3 kez
  tekrarlanınca ~%67-76 başarı oranı gözlendi (bkz. §1.3)
Kanıt dosyaları: evidence/tufe-dsd.md, evidence/tufe-data-query.md,
  evidence/tufe-completion-tests.md, evidence/sdmx-auth-401.md,
  evidence/nsiws-rate-limit-davranisi.md, evidence/nsiws-yeni-token-akisi-2026-09-09.md
Kanıt seviyesi: A
Sonuç: Resmi, en geniş kapsamlı servis; ama üretimde token yönetimi ve
  yüksek, öngörülebilir hata oranı için retry mantığı gerektiriyor.
```

**Kimlik doğrulama akışı (RULES.md §10 uyarınca detaylandırılmıştır):**
- Token endpoint: `POST https://giris.tuik.gov.tr/realms/web/protocol/openid-connect/token`
- `grant_type=password&client_id=nsi-ws-consumer&api_key={API_KEY}`
- Dönen JWT'nin ömrü ~5 dakika — üretimde her istekte veya kısa aralıklarla
  yenilenmesi gerekir.
- API key, TÜİK Veri Portalı hesabı üzerinden SMS doğrulamalı olarak
  üretiliyor (dokümantasyon kanıtı, gerçek üretim bu araştırmada
  yapılmadı — B seviyesi).
- Anahtar, projede yalnızca `secrets/tuik_api_key.txt` içinde tutuldu;
  hiçbir evidence dosyasına yazılmadı.

**DSD yapısı örneği (TÜFE, `DSD_TUFE` v1.12):** 11 boyut sırayla —
`REF_AREA` (CL_IBBS v1.2), `FREQ` (CL_SIKLIK v1.0), `SINIFLAMA_DUZEYI`
(CL_COICOP_HARCAMA_GRUP_DUZEY v1.0), `DEGISIM` (CL_DEGISIM v3.4),
`OZEL_KAPSAM_TUFE` (CL_OZEL_KAPSAMLI_TUFE_GOSTERGELERI v3.0), `BASE_PER`
(CL_YIL v2.0), `YAYIM_DONEMI` (CL_YAYIM_DONEMI v1.0), `COICOP_1999`
(CL_COICOP1999 v1.0), `COICOP_2018` (CL_COICOP2018 v1.0), `INDICATOR`
(CL_GOSTERGE v9.9), `TIME_PERIOD`; primary measure `OBS_VALUE`;
attributes `UNIT_MEASURE`/`CONF_STATUS`/`TIME_FORMAT`/`UNIT_MULT`/
`OBS_STATUS`/`DECIMALS`. (Kanıt: `evidence/tufe-dsd.md`, A.)

### 1.2 databrowser2 JSON-stat/export API (token'sız alternatif — önerilen birincil yol)

```text
İddia: TÜİK'in resmi veri tarayıcı arayüzünün arkasındaki JSON API,
       hiçbir Bearer token/API key göndermeden gerçek veri döndürüyor;
       nsiws'in kapsadığı dataflow'ların büyük kısmında çalışıyor.
Kaynak sayfa: https://databrowser2.tuik.gov.tr/#/tr/tuik/categories/...
Araştırma tarihi: 08.09.2026
Browser işlemi: Chrome DevTools MCP ile sayfa açma + network isteği izleme
İstek yöntemi: POST
İstek URL'si: https://databrowser2.tuik.gov.tr/api/core/nodes/1/datasets/TR,{DATAFLOW_ID},{VERSION}/data
Query parametreleri: (yok, filtre body'de)
Request body: [{"id":"REF_AREA","filterValues":["TR"],"type":"CodeValues","period":0}]
Gerekli header/cookie: yok
HTTP durum kodu: 200 (veri var) / 200 boş `{"id":[],"size":[],"value":{}}`
  (varsayılan REF_AREA kodu o DSD'de seçilebilir değilse) / 500
  `DATAFLOW_NOT_FOUND` (yanlış sürüm numarası kullanılırsa)
Response content-type: application/json (JSON-stat formatı)
Response içindeki ilgili alan: "value" (gözlem değerleri, string-indexed dict)
Gerçek örnek değer: TÜFE için 260 dönemlik endeks/değişim serisi
  ("value":{"0":"3.596523",...})
Tekrar üretilebilirlik: nsiws ile aynı ~%25 sessiz-timeout paterni
  paylaşıyor ama genel başarı oranı çok daha yüksek (114 örneklemde 110 OK)
Kanıt dosyaları: evidence/sdmx-auth-401.md, evidence/databrowser2-genel-erisim-ve-revizyon.md,
  evidence/hatali-kayitlarin-gercek-durumu.md, evidence/kalan-114-dataflow-taramasi.md,
  evidence/databrowser2-export-formatlari-ve-baz-yili.md
Kanıt seviyesi: A
Sonuç: Resmi SDMX dokümantasyonunda tanımlı DEĞİL (TÜİK'in kendi frontend'i
  için iç kullanım amaçlı olabilir), ama fiilen çalışıyor ve geniş test
  edildi. Üretim connector'ı için birincil aday.
```

**Export format endpoint'leri (aynı token'sız erişim, ayrıca doğrulandı):**
- `POST .../download/genericdata` → `application/vnd.sdmx.genericdata+xml` (SDMX 2.1 Generic XML)
- `POST .../download/jsondata` → `application/vnd.sdmx.data+json`, gerçek
  **SDMX-JSON 2.1** şeması (`data.dataSets[].series` yapısı — şema:
  `sdmx-json-data-schema.json`). **Bu, JSON-stat DEĞİL** — §1.2'nin asıl
  JSON-stat kaynağı `nodes/1/datasets/{id}/data` endpoint'idir (yukarıdaki
  ana istek), `/download/jsondata` değil. 14.09.2026'da tam body yakalanıp
  KARVEN'in yeni `parse_sdmx_json()` parser'ına verildi: 1.300 gözlemin
  tamamı doğru ayrıştırıldı ve aynı TÜFE serisinin `/download/csv`
  fixture'ıyla (§1.2 altında) birebir aynı sayısal değerleri döndürdüğü
  çapraz doğrulandı (A). Kanıt:
  `evidence/databrowser2-resmi-sdmx-csv-ve-jsondata-body-yakalama.md`.
  Fixture: `tests/fixtures/tuik/tufe-tt10-download.raw.sdmxjson.json`
  (bkz. §3). `parse_json_stat()` ile karıştırılmamalı — connector'da ayrı
  bir `"sdmx_json"` format kodu var (`databrowser-sdmx-json` kaynağı).
- `POST .../download/csv` → `application/vnd.sdmx.data+csv` (resmi SDMX-CSV formatı, header:
  `DATAFLOW,REF_AREA,FREQ,...,OBS_VALUE,UNIT_MEASURE,CONF_STATUS,TIME_FORMAT,UNIT_MULT,OBS_STATUS,DECIMALS`).
  **Request body Excel export'unkinden farklı** — nesne değil, doğrudan
  filtre kriteri dizisi bekliyor: `[{"id":"REF_AREA","filterValues":["TR"],
  "type":"CodeValues","period":0}]` (aynı `jsonstat_body()` şekli).
  14.09.2026'da tam response body (1.300 satır, 2005-01–2026-08 TÜFE)
  yakalandı ve `parse_sdmx_csv()`'e kod değişikliği gerekmeden verildi,
  1.300 gözlemin tamamı doğru ayrıştırıldı (A). Fixture:
  `tests/fixtures/tuik/tufe-tt10-download.raw.csv` (bkz. §3).

**Kritik uyarı — varsayılan `REF_AREA=TR` her DSD'de geçerli değil:**
Bölgesel (NUTS3/İBBS3) dataflow ailelerinde (`DF_BR_FAALIYET_BUYUKLUK_GIRISIM_IBBS3_*`)
`"TR"` kodu `isSelectable:false` — yalnızca 81 il kodu (`TR100`...`TRC33`)
geçerli. Bu üç dataflow için `PartialCodelists/REF_AREA` sorgusuyla geçerli
kod listesi alınıp 81 ayrı istek yapılması gerekti (243/243 istek %100
başarıyla tamamlandı, bkz. `evidence/kalan-114-dataflow-taramasi.md`).
Connector, her dataflow için REF_AREA varsayımını sabit kodlamamalı,
gerekirse `PartialCodelists` ile geçerli kod setini keşfetmeli.

### 1.3 Sunucu tarafı hata/gecikme davranışı (nsiws + databrowser2 ortak)

```text
İddia: TÜİK'in SDMX backend'i (nsiws ve databrowser2 ortak), art arda
       gelen isteklerde HTTP 429 DÖNDÜRMÜYOR; bunun yerine sabit,
       öngörülebilir ~%25 oranında sessiz timeout (~20s) üretiyor.
Kaynak sayfa: (API davranışı, sayfa yok)
Araştırma tarihi: 08-09.09.2026
Browser işlemi: PowerShell script ile 30/99/20'lik üç ayrı ardışık istek serisi
İstek yöntemi: GET (nsiws, gerçek Bearer token ile)
İstek URL'si: https://nsiws.tuik.gov.tr/rest/data/TR,DF_TUFE_SDMX_TT10,1.0/TR.M.TUFE.1._Z.2025.2026_01._Z.0.F_TFE
Query parametreleri: startPeriod=2025-01&endPeriod=2025-12
Request body: (yok)
Gerekli header/cookie: Authorization: Bearer {token}
HTTP durum kodu: 200 / timeout (~20000ms, HTTP kodu yok)
Response content-type: application/vnd.sdmx.genericdata+xml
Response içindeki ilgili alan: (başarılı isteklerde) OBS_VALUE
Gerçek örnek değer: 30 istek → 20 başarı (%67), 10 timeout (%33);
  99 istek/15dk (4s aralıkla) → 75 başarı (%75.8), 24 timeout (%24.2);
  20 istek (1s aralıkla) → 12 başarı (%60), 8 timeout (%40)
Tekrar üretilebilirlik: Çok yüksek — üç bağımsız ölçümde de "her ~5
  istekten 2'si" deterministik paterni (istek SIRA NUMARASINA göre,
  zamana göre değil) tekrarlandı
Kanıt dosyaları: evidence/nsiws-rate-limit-davranisi.md
Kanıt seviyesi: A
Sonuç: Klasik zaman-penceresi rate-limit modeli DEĞİL — sunucu tarafı
  yük dengeleyici/node havuzu doygunluğuyla tutarlı bir örüntü (bazı
  backend node'lar yanıt vermiyor). Öngörülebilir ve zamanla kötüleşmiyor
  (15 dk boyunca sabit kaldı) — bu üretim için iyi haber: basit 1-2
  tekrar-deneme mantığı güvenle kullanılabilir.
```

**Önemli düzeltme (yanlış-negatif tespiti):** Toplu tarama loglarında
404/timeout olarak işaretlenen 114+7 = 121 dataflow'un **131'i aslında
gerçek veri içeriyordu** (110/114 + 7/7 + 3/3 İBBS3 sonradan çözüldü);
tek kalıcı, çözülemeyen istisna `DF_GENEL_DEVLET_ANA_BILESENLERI_CEYREKLIK`
(tutarsız davranış: bazen 1 saniyede boş 204, bazen 3+ dakika hiç yanıt yok
— D seviyesinde açık kaldı, bkz. §10).

### 1.4 Turcat — IMF SDDS Ulusal Veri Sayfası

```text
İddia: www.tuik.gov.tr/Turcat, IMF SDDS standardında Türkiye'nin resmi
       Ulusal Veri Yayımlama Sayfasını sunan, token'sız, WAF'sız,
       DSBB'ye doğrudan bağlı bir özet gösterge kaynağı.
Kaynak sayfa: https://www.tuik.gov.tr/Turcat (ana site "Erişim" menüsünden bulundu)
Araştırma tarihi: 12.09.2026
Browser işlemi: Gerçek Chrome tarayıcı oturumu (claude-in-chrome) ile
  E-Hizmetler menüsünden gezinme, sayfa kaynağından AJAX endpoint çıkarımı,
  her biri izole curl ile bağımsız doğrulama
İstek yöntemi: GET
İstek URL'si: https://www.tuik.gov.tr/Turcat/ViewTurcatPageReel (+ Mali/Finans/Dis/Nufus)
Query parametreleri: (yok)
Request body: (yok)
Gerekli header/cookie: yok
HTTP durum kodu: 200 (5/5 sektör endpoint'i)
Response content-type: application/json
Response içindeki ilgili alan: EN_SON_YAYIMLANAN_VERI, BIR_ONCEKI_DONEM_VERI, METAVERI (IMF DSBB linki)
Gerçek örnek değer: Nüfus 2025 = "86 092" bin kişi (önceki: "85 665");
  GSYH cari fiyat D2/26 = 19.869.747.341 bin TL; Uluslararası Rezervler
  04/Eyl/26 = 184.246,6 milyon USD
Tekrar üretilebilirlik: Yüksek — hem browser hem izole curl'de bağımsız doğrulandı
Kanıt dosyaları: evidence/2026-09-12-turcat-ulusal-veri-sayfasi-kesfi.md
Kanıt seviyesi: A
Sonuç: 258 satırda TÜM kilit makro göstergelerin en güncel + önceki dönem
  değeri, token'sız tek GET ile. Geçmiş seri YOK — sadece anlık görüntü.
```

**GÜNCELLEME (14.09.2026) — connector default source düzeltildi:**
`default_sources()`'taki `turcat-summary` kaynağı yanlışlıkla
`https://www.tuik.gov.tr/Turcat` (HTML sarmalayıcı sayfa, JSON değil) URL'sine
işaret ediyordu — canlı denendiğinde `parse_turcat()` `TuikParseError: invalid
Turcat JSON` ile başarısız oluyordu. Gerçek, dokümante edilmiş sektör
endpoint'lerinden biri (`.../Turcat/ViewTurcatPageNufus`) kullanılacak şekilde
düzeltildi ve canlı doğrulandı (HTTP 200, 560 byte, 1 belge — Nüfus 2025 =
86.092 bin kişi). Diğer sektörler (`ViewTurcatPageReel`/`Mali`/`Finans`/`Dis`)
aynı şekilde çalışır ama şu an yalnızca `Nufus` default source'a bağlandı.

### 1.5 Basın bülteni API'si (WAF davranışı düzeltildi)

```text
İddia: data.tuik.gov.tr/veriportali basın bülteni API'si, gerçek (headless
       olmayan) tarayıcı oturumundan %100 güvenilir çalışıyor; önceki
       "tamamen WAF-blokeli" sonucu yanlış-negatifti.
Kaynak sayfa: https://veriportali.tuik.gov.tr/tr/press/{id} (kullanıcı
  tarafından 2 örnek verildi: 58188, 58021)
Araştırma tarihi: 12.09.2026
Browser işlemi: Gerçek, kullanıcının ön plana getirdiği Chrome tab'i
İstek yöntemi: GET
İstek URL'si: https://veriportali.tuik.gov.tr/tr/press/{id}
Query parametreleri: (yok, path parametresi)
Request body: (yok)
Gerekli header/cookie: yok (ama headless/otomasyon fingerprint'i 403 tetikliyor)
HTTP durum kodu: 200 (gerçek görünür tarayıcı) / 403 (headless/curl/bare otomasyon)
Response content-type: text/html (+ ilişkili API çağrıları JSON)
Response içindeki ilgili alan: bülten başlığı, tarih, ilişkili PDF/tablo linkleri
Gerçek örnek değer: press/58188 ve press/58021 ikisi de gerçek, dolu bülten
  sayfası olarak doğrulandı
Tekrar üretilebilirlik: Görünür tarayıcı oturumunda yüksek; headless'ta sürekli 403
Kanıt dosyaları: evidence/2026-09-12-press-api-gercek-tarayicidan-calisiyor.md
Kanıt seviyesi: A
Sonuç: WAF, User-Agent/otomasyon fingerprint'ine bakıyor, IP/rate-limit'e
  değil. Connector normal bir Chrome UA string'iyle (headless Chrome DEĞİL,
  gerçek/headed veya iyi taklit edilmiş fingerprint) çalıştırılmalı.
```

**Production'a aktarım notu — sadece 2 veri noktası test edildi, aradaki
boşluk test edilmedi:** Bu araştırmada denenen tek iki uç şu: (1) çıplak
`curl`/headless otomasyon → 403, (2) gerçek, görünür (headed) Chrome
oturumu → 200. **Aradaki orta yol hiç test edilmedi:** Playwright/Puppeteer
gibi bir araçla, gerçekçi `User-Agent` + `sec-ch-ua` header'ları ve
stealth-eklentili (otomasyon fingerprint'ini gizleyen) **headless** bir
Chrome instance'ının bu WAF'ı geçip geçemeyeceği bilinmiyor (D seviyesi).
Bu, production connector tasarımını doğrudan etkiliyor: eğer stealth-headless
yeterliyse hafif bir sunucu-taraflı otomasyon yeterli olur; yeterli değilse
sunucuda sanal ekranlı (Xvfb vb.) gerçek/headed bir Chrome process'i sürekli
çalıştırmak gerekir — bu çok daha ağır bir altyapı gereksinimi. Bu ayrım
§8 ve §10'da da işaretlenmiştir.

**GÜNCELLEME (14.09.2026) — endpoint artık 404 veriyor, muhtemelen TÜİK
tarafı değişti:** `default_sources()`'taki `press-api` kaynağı önce yanlış
host'a (`data.tuik.gov.tr`) işaret ediyordu — bu, `veriportali.tuik.gov.tr/
api/tr/press` olarak düzeltildi (araştırmada 12.09.2026'da A seviyesinde
doğrulanmış gerçek host/path). Ancak **canlı yeniden denendiğinde
(14.09.2026) hem liste hem detay endpoint'i 404 döndürdü** — sadece 2 gün
önce çalışan bir API artık yok/taşınmış görünüyor. Bu bir URL hatası değil,
gerçek bir upstream değişikliği; connector şu an doğru ama şu anda
işlevsiz bir URL'e işaret ediyor. Task 1.9'da bu kaynak yeniden
araştırılmadan production'a alınmamalı.

**ÇÖZÜLDÜ (02.10.2026, canlı ölçüm, Task 1.2h) — tarayıcı gerekmiyor:**
Yukarıdaki "gerçek Chrome şart" ve "endpoint 404'e düştü" sonuçları yanlıştı.
WAF yalnızca iki başlığa bakıyor:

| İstek | Sonuç |
|---|---|
| Başlıksız (`python-httpx` UA) | 403 `Erişim engellendi` |
| Chrome `User-Agent` | dosya uçları 200, JSON API'ler 404 `Sayfa bulunamadı` |
| Chrome `User-Agent` + `X-Requested-With: XMLHttpRequest` | hepsi 200 |

İkinci başlık portalın kendi axios istemcisinin varsayılanı (JS bundle'da
`headers.common["X-Requested-With"]="XMLHttpRequest"`). 14.09'daki 404 bu
başlığın eksikliğiydi. Düz `httpx` yeterli; Playwright/Xvfb gerekmez.

Doğrulanan uçlar (hepsi düz HTTP):
- `GET /api/tr/press` (170 kayıt, her bülten türünün güncel sayısı),
  `/api/tr/press/latest` (50), `/api/tr/press/indicators` (6 kilit gösterge).
- `GET /api/tr/press/{id}` → `id,date,number,title,period,content (HTML),
  statisticalTables (databrowser2 `DF_` bağlantıları),tables (xls),reports
  (pdf),metadatas,previousPresses (son 12)`. Olmayan id →
  `{"isError":true,"message":"Bülten bulunamadı"}`.
- Bülten id listesi: `www.tuik.gov.tr/Kurumsal/GetYillikHaberBulteniListesi?yil=Y`
  (WAF yok) → `yayindaOlanlarList` / `yayindaOlmayanlarList`; 33 kurumun
  ulusal veri takvimi (yaklaşan yayın tarihleri dahil). TÜİK: 2005–2026,
  yılda ~200–400 bülten; 2006, 2010, 2014, 2018 örnek id'leri yeni API'de 200.
- `GET /api/tr/dataflows` → 510 kayıt (342 indirilebilir), alanlar `id,name,
  version,description,period,updatedAt,downloadable,category,footnotes`.
  Canlı katalogdaki 465 `DF_` veri setinin tamamı burada; fazladan 43 kayıt
  var, hiçbiri indirilebilir değil.
- `GET /api/tr/dataflows/{id}/file/{csv|json|xml}`, `POST
  /api/tr/dataflows/bulk-downloads` `{"dataflows":[…],"formats":["csv"]}` →
  iş id → `GET …/bulk-downloads/{id}/file` (ZIP). `…/{id}/metadata` 404.
- Bülten Excel/PDF dosyaları: `/api/tr/data/downloads?t=t|r&pid=…&p=…`.

Hız (02.10.2026): `/api/tr/data/downloads` **IP başına 5 sn'de 1 dosya**
(oturum/çerez fark etmez); daha sık → HTTP 200 `text/html` "Yönlendiriliyor…
5 saniye sonra tekrar dosya indirebilirsiniz". JSON API'lerde 8 paralele
kadar yavaşlatma görülmedi; sınır sunucu gecikmesi (bülten detayı ~150
istek/dk), 1–2 paralel yeterli.

### 1.6 MEDAS + CİP GetMapData proxy (bölgesel istatistikler)

```text
İddia: MEDAS (ZK Framework tabanlı bölgesel istatistik uygulaması) 92
       konunun tamamını kapsıyor; CİP GetMapData, bu verilere token'sız
       erişim sağlayan bir proxy.
Kaynak sayfa: biruni.tuik.gov.tr/medas/ ; CİP GetMapData endpoint'i
Araştırma tarihi: 08-09.09.2026 (bu oturumdan önceki, önceki sprint'te tamamlandı)
Browser işlemi: ZK postback (zkau) ile UI otomasyonu + doğrudan proxy isteği
İstek yöntemi: GET/POST (uygulamaya göre değişir)
İstek URL'si: (CİP proxy) GetMapData?...&duzey={1-5}
Query parametreleri: duzey (1=ülke...4=ilçe, dokümante edilmemiş; 5 geçerli
  görünse de hiç veri döndürmüyor)
Request body: (uygulamaya göre)
Gerekli header/cookie: yok (CİP proxy token'sız)
HTTP durum kodu: 200
Response content-type: JSON
Response içindeki ilgili alan: gösterge değerleri, coğrafi birim kodları
Gerçek örnek değer: (92/92 konu MEDAS'ta tam tarandı — konu genişletme
  otomasyonu kullanıcı talebiyle durduruldu, mevcut 92/92 veri kullanılacak)
Tekrar üretilebilirlik: Yüksek
Kanıt dosyaları: tuik/medas/ altındaki ~20 dosya (konu bazlı tarama kayıtları)
Kanıt seviyesi: A (92/92 konu), D (ilçe/duzey=4 detay genişletmesi — kullanıcı
  talebiyle iptal edildi, mevcut veriyle yetinildi)
Sonuç: `duzey=5` hiçbir zaman veri döndürmedi (dokümante edilmemiş sınırlama).
  Eksik gözlem birimleri her zaman `null` olarak işaretlenmeli, `0` ile
  KARIŞTIRILMAMALI — API seviyesinde gerçek sıfır ile gizlilik nedeniyle
  bastırılmış değeri ayırt etmek kalıcı olarak mümkün değil.
```

**GÜNCELLEME (14.09.2026) — gerçek endpoint ve düzeltilmiş `default_sources()`:**
Gerçek CİP proxy host'u `cip.tuik.gov.tr` (`data.tuik.gov.tr` DEĞİL), path
`/Home/GetMapData`, zorunlu parametreler `kaynak` (`medas`/`ilGostergeleri`/
`json`), `duzey`, `gostergeNo`, `kayitSayisi`, `period` — örnek: `https://
cip.tuik.gov.tr/Home/GetMapData?kaynak=medas&duzey=3&gostergeNo=ADNKS-GK137473-O29001&kayitSayisi=5&period=yillik`.
Connector'daki `default_sources()`'ta `cip-regional`/`medas-regional`
kaynakları önceden yanlış host'a (`data.tuik.gov.tr`) ve eksik parametrelere
işaret ediyordu (canlı denendiğinde 404 dönüyordu) — `cip_map_data_url()`
helper'ı eklenip düzeltildi, gerçek connector koduyla (`TuikHttpFetcher`)
canlı test edildi: 405 satır (81 il × 5 yıl) doğru şekilde
`institution`/`indicator_definition`/`data_series`/`data_vintage`'a
dönüştü. Fixture: `tests/fixtures/tuik/cip-medas-nufus-duzey3.raw.json`.
Kanıt: `evidence/cip-kaynak-parametresi-kesfi.md`.

**GÜNCELLEME (14.09.2026, Codex derin incelemesi sonrası) — `medas-regional`
kaldırıldı, birebir aynı kaynağı ikinci kez çekip mükerrer satır
üretiyordu:** `cip-regional` ile `medas-regional` aynı URL/indikatörü
kullanıyordu; ikisi birlikte çalıştırıldığında aynı 405 gözlem iki kez
işlenip neredeyse birebir aynı (yalnızca `vintage_timestamp` farklı)
mükerrer `data_vintage` satırları üretiyordu. `medas-regional` kaynağı
silindi; farklı, gerçekten ayrı bir MEDAS göstergesi doğrulanırsa ayrı
bir kaynak olarak eklenmeli.

### 1.7 Excel export ve PDF yayınları (format kapsamı — 13.09.2026'da A seviyesine yükseltildi)

```text
İddia: databrowser2'nin Excel export'u gerçek, indirilebilir bir dosya
       üretiyor; TÜİK'in duyuru/infografik PDF'leri gerçek, makine-
       okunabilir metin içeriyor (taranmış görüntü değil).
Kaynak sayfa: https://databrowser2.tuik.gov.tr/#/tr/tuik/categories/6/6_5/TR,DF_TUFE_SDMX_TT10,1.0
  (Excel); https://www.tuik.gov.tr/media/announcements/Dng2026_tr.pdf (PDF)
Araştırma tarihi: 13.09.2026
Browser işlemi: Gerçek browser oturumunda "Dışa aktar → Excel" tıklanarak
  gerçek istek body'si (sayfanın kendi XHR'ı geçici enstrümante edilerek)
  yakalandı; PDF doğrudan izole GET ile indirildi
İstek yöntemi: POST (Excel) / GET (PDF)
İstek URL'si: https://databrowser2.tuik.gov.tr/api/core/nodes/1/export/excel/false
  (önceki tahmin `/download/excel` YANLIŞ çıktı — gerçek endpoint bu);
  https://www.tuik.gov.tr/media/announcements/Dng2026_tr.pdf
Query parametreleri: (yok)
Request body: (Excel) `{"data":{datasetId, dataCriterias, nodeId},
  "layout":{rows, cols, filters, filtersValue}, "parameters":{biçimlendirme}}`
  — CSV/XML/JSON'dan farklı olarak dataflow'a özgü tablo layout'u da
  body'de taşınıyor
Gerekli header/cookie: yok (ikisi de token'sız)
HTTP durum kodu: 200 (ikisi de)
Response content-type: application/vnd.ms-excel (Excel); application/pdf (PDF)
Response içindeki ilgili alan: (Excel) ZIP/XLSX imzası `50 4B 03 04`;
  (PDF) gömülü metin katmanında gerçek sayısal değerler
Gerçek örnek değer: Excel — 15.485 byte, gerçek geçerli XLSX; PDF —
  1.636.282 byte, 2 sayfa, "Türkiye nüfusu 85.664.944", "toplam
  doğurganlık hızı 1,42" gibi gerçek TÜİK verileri metin olarak çıkarıldı
Tekrar üretilebilirlik: Yüksek (ikisi de tek denemede başarılı)
Kanıt dosyaları: evidence/format-testleri/excel-ve-pdf-format-dogrulama.md,
  evidence/format-testleri/Dng2026_tr.pdf (SHA-256:
  2993f6ba395fcd048eb216b1ddc80fecceb6495f8781806c0e09fc6af3717706)
Kanıt seviyesi: A
Sonuç: Her iki format da gerçek ve kullanılabilir, ama ikisi de XML/JSON/
  CSV'ye göre EK ayrıştırma karmaşıklığı getiriyor: Excel export'u her
  dataflow için doğru `layout`/`filtersValue` kombinasyonu türetilmesini
  gerektiriyor (sabit şablon CSV/XML/JSON gibi her dataflow'a doğrudan
  uygulanamıyor — D seviyesinde açık, bkz. §10); PDF verisi tablo/şema
  değil serbest metin/infografik düzeninde geldiği için yapılandırılmış
  çıkarım ek ayrıştırma mantığı (düzen-bazlı veya regex) gerektiriyor.
  Bu iki format, connector öncelik sıralamasında (§8) XML/JSON/CSV'den
  SONRA gelmeli.
```

**GÜNCELLEME (14.09.2026) — `default_sources()`'taki `official-excel`/
`official-pdf` canlı 404 veriyor, bilinçli placeholder:** Bu iki kaynağın
URL'leri (`.../api/core` ve `.../media/announcements`, ikisi de path
sonu eksik) canlı test edildiğinde 404 döndü. Bu bir hata DEĞİL, kasıtlı
bir eksiklik: Excel export'u yukarıda açıklandığı gibi her dataflow için
farklı bir `layout` body'si gerektiriyor (sabit tek URL olamaz), PDF ise
her duyuru için ayrı bir dosya adı gerektiriyor (`Dng2026_tr.pdf` gibi
tek bir örnek var, "güncel duyuru" diye genel bir endpoint yok). Bu iki
kaynağın gerçek anlamda çalışması için Task 1.9'da her biri için
somut, göstergeye özel konfigürasyon eklenmesi gerekiyor — genel bir
URL düzeltmesiyle çözülemez.

### 1.8 Sınıflama Sunucusu (`siniflama.tuik.gov.tr`) — resmi sınıflama hiyerarşisi kaynağı

```text
İddia: Sınıflama Sunucusu, TÜİK'in kullandığı resmi sınıflamaların
       (ISIC, NACE, İBBS/NUTS, COICOP dahil çok sayıda sınıflama sistemi)
       kod/tanım/üst-kod hiyerarşisini token'sız, WAF'sız JSON API
       üzerinden döndürüyor.
Kaynak sayfa: https://siniflama.tuik.gov.tr/
Araştırma tarihi: 08.09.2026, 14.09.2026 (canlı doğrulama tekrarı)
İstek yöntemi: GET
İstek URL'leri:
  - Sınıflama listesi: GetSiniflamalarSahibi?tur={tur}&searchname=null
  - Kod ağacı (tüm hiyerarşi, üst kod dolu): GetSiniflamaSatir?surumId={id}&seviye=1&kod=0
  - Tek kod (hedefli sorgu, üst kod boş döner): GetSiniflamaSatir?surumId={id}&seviye={n}&kod={kod}
Gerekli header/cookie: X-Requested-With: XMLHttpRequest; Authorization/API key yok
HTTP durum kodu: 200
Response content-type: application/json; charset=utf-8
Response içindeki ilgili alan: kod, ust_kod, tanim, tanim_en, duzey
Gerçek örnek değer (ISIC Rev.4, tam ağaç, surumId=198, seviye=1&kod=0):
  kod="011" → ust_kod="01"; kod="0111" → ust_kod="011"; kod="01" → ust_kod="A"
Kanıt seviyesi: A — hem 08.09.2026 browser oturumunda hem 14.09.2026
  doğrudan canlı istekle iki kez bağımsız doğrulandı.
Kanıt dosyaları: evidence/siniflama-sunucusu.md,
  evidence/raw/2026-09-08-siniflama-GetSiniflamaSatir-0111.json (hedefli, ust_kod=null),
  evidence/raw/2026-09-14-siniflama-isic198-seviye1-tree.json (tam ağaç, 766 satır, ust_kod dolu)
Sonuç: `ust_kod` alanı yalnızca **ağaç yürüyüşüyle** (seviye=1, kod=0'dan
  başlayarak) dolu geliyor; tek bir kod için hedefli sorgu (`kod=0111` gibi)
  `ust_kod: null` döndürüyor. KARVEN connector'ı sınıflama hiyerarşisini
  (Faz 2'nin `COMPONENT_OF` ilişkisi için) çekerken ağaç yürüyüşünü
  kullanmalı, hedefli tek-kod sorgusunu değil.
```

**Coğrafi sınıflama sınırlaması:** Sınıflama Sunucusu'nda İBBS 2005'in
yalnızca 81 il (`duzey=4`) satırı erişilebilir; 973 ilçelik idari/NUTS4
kırılımı bu serviste YOK (CİP'in `nuts4.json`'ıyla karşılaştırıldı, kod/ad
kesişimi 0/973). Kanıt: `evidence/siniflama-sunucusu.md` §"09.09.2026".

### 1.9 Biruni Yayın Sistemi (`biruni.tuik.gov.tr/yayin`) — yayın kataloğu, 632 kayıt

```text
İddia: Yayın Sistemi, ZK 7 AU (asynchronous update) protokolü üzerinden
       çalışan bir yayın/rapor/mikro-veri-seti kataloğu sunuyor; 632 kayıt,
       71 sayfa (sayfa başı 9 kayıt), token'sız erişilebiliyor.
Kaynak sayfa: https://biruni.tuik.gov.tr/yayin/views/visitorPages/index.zul
Araştırma tarihi: 09.09.2026 (UI akışı), 14.09.2026 (connector doğrulaması)
İstek yöntemi: GET (bootstrap) + POST (sayfalama, AU protokolü)
İstek URL'leri: bootstrap `.../index.zul`; sayfalama
  `https://biruni.tuik.gov.tr/yayin/zkau` — body
  `dtid=<desktop_id>&cmd_0=onPaging&uuid_0=<paging_uuid>&data_0={"":<sayfa-1>}`
Gerekli header/cookie: bootstrap GET'in Set-Cookie'leri + `zk-sid` header'ı
  (ilk yanıttan alınır, yoksa "1"); `Authorization`/API key yok
HTTP durum kodu: 200
Response content-type: text/html (bootstrap) / text/plain;charset=UTF-8 (AU)
Response içindeki ilgili alan: `zul.sel.Listbox`/`zul.sel.Listitem` bileşen
  ağacı — `label` (konu/başlık/tür), `href` (`yayin_no=` parametreli)
Gerçek örnek değer: `yayin_no=725` → "Hanehalkı Bütçe İstatistikleri Mikro
  Veri Seti, 2025"; `yayin_no=723` → "İstatistik Okuryazarlığı: Resmî
  İstatistikleri ve Veriyi Doğru Okumak" (yıl etiketi yok, `published_at`
  bilinçli olarak `null`)
Tekrar üretilebilirlik: 09.09.2026'da tam 632 kayıt taranmış
  (`yayin-metadata.ndjson`); 14.09.2026'da KARVEN'in gerçek connector koduyla
  (`TuikHttpFetcher`) 3 sayfa (27 kayıt) bağımsız olarak yeniden çekildi ve
  aynı kayıtlar (725/724/723) doğrulandı.
Kanıt dosyaları: evidence/2026-09-09-tuik-yayin-sistemi-ve-duyuru-pdfleri.md,
  evidence/yayin-sistemi-httpx-connector-dogrulama.md,
  evidence/raw/2026-09-14-yayin-index.initial.html,
  evidence/raw/2026-09-14-yayin-page2.au-response.txt
Kanıt seviyesi: A
Sonuç: Kataloğun tamamı **düz HTTP (httpx) ile, tarayıcı/Playwright
  gerekmeden** otomatikleştirilebiliyor — bu, mimari olarak "browser
  otomasyonu gerektirir" şeklindeki önceki değerlendirmeyi düzeltiyor.
  `robots.txt` bu host'ta kararsız (503) olduğundan bu kaynak için
  robots.txt kontrolü atlanıyor (nsiws'in token-doğrulanmış kaynaklarıyla
  aynı mantık). Katalog kayıtları sayısal gösterge değil, **belge/yayın
  metadata'sı** — `source_document` modeline eşleniyor, `data_vintage`'e
  değil.
```

**Bilinen sınırlama:** Katalog yalnızca başlık/konu/tür/yıl/link metadata'sı
veriyor; yayının kendisinin (PDF/mikro veri seti dosyası) indirilmesi ayrı,
her yayın için farklı bir eylem gerektiriyor (§1.9 bu akışı kapsamıyor,
yalnızca katalog taranıyor).

### 1.10 Seçim Dağıtım Sistemi (`biruni.tuik.gov.tr/secimdagitimapp`) — kısmen otomatikleştirildi

```text
İddia: Milletvekili genel seçimi sonuçları (1950'ye kadar, 10 farklı tablo
       kırılımı) token'sız, WAF'sız bir ZK form-sihirbazı + üretilmiş
       HTML rapor akışıyla erişilebiliyor.
Kaynak sayfa: https://biruni.tuik.gov.tr/secimdagitimapp/secim.zul
Araştırma tarihi: 09.09.2026 (UI keşfi), 14.09.2026 (otomasyon denemesi)
Kanıt seviyesi: A (erişim ve rapor içeriği için), B (otomasyon kapsamı için
  — 10 tablodan yalnızca 1'i uçtan uca otomatikleştirildi)
Sonuç: Rapor üretimi gerçek, tekrarlanabilir ve token'sız — ama 9/10 tablo
  ZK'nin özel Listbox/Combobox widget'larını (native HTML form kontrolü
  değil) zorunlu alan olarak kullanıyor; bu, genel "ilk seçeneği işaretle"
  otomasyon stratejisiyle doldurulamıyor. Yalnızca "Milletvekili genel
  seçimi sonucu" (yalnızca yıl radio'su gerektiren, en basit tablo) uçtan
  uca doğrulandı.
```

**Rapor formatı:** `rapory.tuik.gov.tr/{tarih-saat}-{oturuma-özel-id}.html`,
`windows-1254` kodlamalı, eski tip Excel-HTML exportu (onlarca spacer sütun,
iç içe `colspan`/`rowspan`). Bunun için genel, tablo-tipinden-bağımsız bir
grid çözümleyici yazıldı: `parse_legacy_report_tables()` /
`parse_legacy_report_main_table()` (`app/ingestion/data/tuik/parser.py`) —
`colspan`/`rowspan`'ı bir tarayıcı gibi çözerek yoğun bir 2D grid üretiyor;
hangi sütunun hangi anlama geldiğini BİLMİYOR (bu, tabloya özel bir
yorumlama katmanı gerektirir, henüz yazılmadı).

**Kanıt dosyaları:** `evidence/secim-istatistikleri-kesfi.md`,
`evidence/secim-dagitimapp-playwright-otomasyon-denemesi.md`,
`evidence/raw/2026-09-14-secim-table07-report.raw.html`. Fixture:
`tests/fixtures/tuik/secim-2023-milletvekili-genel.raw.html`.

**Açık kalan iş (bilinçli olarak bu oturumda tamamlanmadı):**
- Kalan 9 tablo tipi için doğru ZK widget seçicilerinin bulunması
  (muhtemelen "seçim çevresi"/"il" alanları için).
- Grid'den anlamlı `data_vintage`/`indicator_definition` satırlarına
  semantik eşleme (şu an yalnızca ham grid, `source_document.metadata.grid`
  içinde saklanıyor — Turcat/Yayın gibi "belge", sayısal gösterge değil).
- turizmapp ve bi.tuik.gov.tr (Qlik BI) hiç otomatikleştirilmedi.

### 1.11 turizmapp (`biruni.tuik.gov.tr/turizmapp`) — 3 konu, tamamı uçtan uca doğrulandı

```text
İddia: Turizm istatistikleri (çıkış yapan yabancı/vatandaş, giriş yapan
       vatandaş, giriş-çıkış yapan ziyaretçi) için ayrı, çalışan bir ZK
       rapor sihirbazı; Seçim Dağıtım Sistemi'yle aynı DHTML AU mimarisini
       kullanıyor.
Kaynak sayfalar: https://biruni.tuik.gov.tr/turizmapp/{cikis,giris,sinir}.zul
Araştırma tarihi: 09.09.2026 (ilk keşif), 14.09.2026 (bağımsız tekrar)
Kanıt seviyesi: A — 3 konunun 3'ü de iki bağımsız oturumda üretildi ve
  byte-birebir aynı sonuçları verdi (47.269 / 95.238 / 582.730 byte).
Sonuç: Rapor üretimi tamamen token'sız, deterministik ve tekrarlanabilir.
  Form doldurma adımları (Toplam/Profil/Yıllık gibi radio grupları +
  "Turizm Değişkenleri" ve "Yıl/Kapı Seçimi" gibi sunucu tarafında dinamik
  doldurulan özel liste kutuları) genel bir otomasyon stratejisiyle
  güvenilir şekilde tamamlanamadı — hedefli/interaktif tıklamayla üretildi.
```

**Parser tekrar kullanımı — önemli bulgu:** Seçim Dağıtım için yazılan
`parse_legacy_report_tables()`/`parse_legacy_report_main_table()`
(§1.10), turizmapp'in 3 raporunun HİÇBİRİNDE kod değişikliği gerektirmeden
doğru çalıştı. Bu, iki farklı ZK uygulamasının (`secimdagitimapp`,
`turizmapp`) aynı rapor-üretim/export motorunu paylaştığını doğruluyor —
bu motoru kullanan başka bir sistem bulunursa (ör. gelecekte keşfedilecek
bir `{konu}app`) aynı parser muhtemelen yine çalışacaktır.

**Kanıt dosyaları:** `evidence/yeni-alt-alan-adlari-ve-uygulamalar-2026-09-09.md`,
`evidence/turizmapp-3-konu-uctan-uca-dogrulama.md`. Fixture'lar:
`tests/fixtures/tuik/turizmapp-{cikis,giris,sinir}-report.raw.html`.

**Açık kalan iş:** Form doldurma adımlarının (özellikle bağımlı `Libox`
listeleri ve sunucu tarafı zorunlu-alan validasyonu — bkz. sinir.zul'daki
"Kapıları seçiniz!!!" hatası) tam otomasyonu; Seçim Dağıtım'ın kalan 9
tablosuyla birlikte ortak bir "ZK legacy DHTML form-state makinesi"
yazılırsa ikisi de aynı anda çözülebilir.

### 1.12 bi.tuik.gov.tr (Qlik BI) — Dış Ticaret İstatistikleri

```text
İddia: `biruni.tuik.gov.tr/disticaretapp/` → `bi.tuik.gov.tr` modern React
       SPA'sına 302 yönleniyor; Qlik Sense tabanlı bir dış ticaret rapor
       sihirbazı (4 ana kategori: Toplam İhracat/İthalat, Ürün/Ürün Grubu-
       Ülke, Ülke ve Ülke Grubu, İllere Göre — sonuncusu 6 alt-sınıflama
       ile HS/BEC/ISIC/SITC kırılımı sunuyor).
Kaynak sayfa: https://bi.tuik.gov.tr/extensions/tuik-mashup/index.html?report_type=2
Araştırma tarihi: 09.09.2026 (ilk keşif, B), 14.09.2026 (protokol çözümü, A)
Kanıt seviyesi: A — yalnızca "Toplam İhracat/İthalat" kategorisi için;
  kalan 3 kategori ve `report_type=1`'in 11 kategorisi B/D.
```

**BÜYÜK BULGU — export endpoint'i veri üretmiyor, yalnızca biçimlendiriyor:**
`POST https://report-bi.tuik.gov.tr/prodapi/exportfile/excel` isteğinin
JSON gövdesi tablo verisinin TAMAMINI (qText/qNum hücreleri olarak) zaten
içeriyor — gerçek veri çekimi Qlik motorunun kendi (muhtemelen WebSocket
tabanlı) engine API'si üzerinden, React SPA içinde, export'tan ÖNCE
gerçekleşiyor. Bu protokol çözülmedi (D) — **ama gerek de yok**: 3 adımlık
wizard'ı (kategori → tarih/ihracat-ithalat/para birimi → "Raporu Oluştur")
otomatikleştirip "Excel" butonuna basmak, gerçek sayısal veriyi içeren
temiz bir XLSX dosyası üretmeye yetiyor.

**Excel çıktısı formatı:** databrowser2'nin (tek period/value çiftli) veya
Seçim/turizmapp'in (spacer-sütunlu legacy HTML) hiçbirine benzemeyen,
temiz/semantik bir yapı: birkaç başlık satırı, boş ayraç, tek bir header
satırı, sonra veri satırları — birden fazla değer sütunu olabiliyor (bu
örnekte İhracat USD + İthalat USD). Bunun için yeni bir genel çözücü
yazıldı: `parse_bi_trade_export_xlsx()`.

**Kanıt dosyaları:** `evidence/yeni-alt-alan-adlari-ve-uygulamalar-2026-09-09.md`,
`evidence/bi-tuik-qlik-export-protokolu-cozumu.md`,
`evidence/raw/2026-09-14-bi-export-capture-summary.json`. Fixture:
`tests/fixtures/tuik/bi-toplam-ihracat-ithalat-2025.raw.xlsx`.

**Açık kalan iş:** Kalan 3 kategori (özellikle "İllere Göre"nin 6 alt-
sınıflaması) ve `report_type=1`'in 11 kategorisi henüz otomatikleştirilmedi.
Her biri muhtemelen aynı 3-adımlı wizard desenini paylaşıyor ama farklı
form alanları istiyor — Seçim/turizmapp'teki gibi konuya özel keşif
gerekiyor.

## 2. Veri sözlüğü

| Alan adı (kaynakta) | Anlamı | Tip/Format | Birim | Zorunlu mu | Örnek gerçek değer |
|---|---|---|---|---|---|
| `REF_AREA` | Coğrafi kapsam kodu (İBBS düzeyi) | Kod (CL_IBBS) | - | Evet | `TR` (ülke), `TR100` (İstanbul, NUTS3) |
| `FREQ` | Gözlem sıklığı | Kod (CL_SIKLIK) | - | Evet | `M` (aylık), `A` (yıllık), `Q` (çeyreklik) |
| `TIME_PERIOD` | Gözlem dönemi | ISO-benzeri dönem string | - | Evet | `2025-01`, `2025`, `2024-Q4` |
| `OBS_VALUE` | Ana ölçüm değeri | Ondalıklı sayı (string) | değişken | Evet | `"3.596523"` |
| `INDICATOR` | Gösterge kodu | Kod (CL_GOSTERGE) | - | Evet | `TUFE`, `II_EFGTG` |
| `UNIT_MEASURE` | Birim | Kod | değişken | Attribute | `bin kişi`, `%`, `milyon TL` |
| `CONF_STATUS` | Gizlilik durumu | Kod | - | Attribute | (gizlilik nedeniyle bastırılmış gözlemi işaretler) |
| `OBS_STATUS` | Gözlem durumu | Kod | - | Attribute | (tahmini/geçici/kesin vb.) |
| `BASE_PER` | Temel dönem/baz yılı | Kod (CL_YIL) | - | Bazı DSD'lerde | `2025` (TÜFE 2025=100) |
| `YAYIM_DONEMI` | Revizyon/yayım dönemi | Kod (CL_YAYIM_DONEMI) | - | Bazı DSD'lerde | `2026_08` |

**Kod listesi / sınıflama hiyerarşisi örneği (TÜFE COICOP-2018, ana seviye
— TÜİK'in genel COICOP-2018 dokümantasyonundan derlenmiş, ayrıca bir
API'den çekilerek doğrulanmamış illüstratif örnek; gerçek, API'den
doğrulanmış üst-kod ilişkisi için bkz. §1.8 ve §4):**

| Kod | Açıklama | Üst kod |
|---|---|---|
| 0 | CPI / TÜFE (genel endeks) | - |
| 01 | Gıda ve alkolsüz içecekler | 0 |
| 02 | Alkollü içecekler, tütün ve tütün ürünleri | 0 |
| 03 | Giyim ve ayakkabı | 0 |
| 04 | Konut, su, elektrik, gaz ve diğer yakıtlar | 0 |
| ... (13 ana grup, 797 kod toplam COICOP-2018 codelist'inde) | | |

Codelist'te bir kodun bulunması, o kodun her dataflow×dönem kombinasyonunda
gerçekten seri olarak var olacağını garanti etmiyor — istemci önce
dataflow/DSD keşfi yapmalı (bkz. `evidence/tufe-completion-tests.md`).

## 3. Gerçek örnek ham veri

Aşağıdaki 16 dosya `tests/fixtures/tuik/` altına taşınmış durumda (RULES.md
§11 onayı alınmış, taşıma tamamlandı):

| Dosya | Boyut | Neyi temsil ediyor | SHA-256 |
|---|---|---|---|
| `tufe-tt10-2025.raw.xml` | 3.125 byte | nsiws SDMX Generic Data XML — gerçek TÜFE serisi (§1.1) | `407b9872b1bd39a5d25f254b5e847025e183e1c4f958a2d8e14da39ab6a46683` |
| `turcat-nufus.raw.json` | 560 byte | Turcat en küçük/en temiz token'sız JSON örneği (§1.4) | `ba6bec8ace8995d5ac6e387cedf8434375d90398ccc5b5874df35d72fb68f4a5` |
| `Dng2026_tr.pdf` | 1.636.282 byte | Gerçek, metin katmanlı TÜİK PDF yayını (§1.7) | `2993f6ba395fcd048eb216b1ddc80fecceb6495f8781806c0e09fc6af3717706` |
| `tufe-tt10-download.raw.csv` | 112.027 byte | databrowser2 `/download/csv` — resmi SDMX-CSV, gerçek TÜFE (1.300 satır, 2005-01–2026-08) (§1.2) | `19abf0289cff402fd7bac2241dc41ecbe6baf906a9e6caf1ecb374bf7a49c37d` |
| `organik-sut.raw.json` | 2.677 byte | databrowser2 asıl JSON-stat API — en küçük gerçek fallback örneği (§1.2) | `f78000ab9c5289759c6965cad6b7d426bccfc7f496bf0f38844d4d10fe0908fd` |
| `tufe-tt10-download.raw.sdmxjson.json` | 68.056 byte | databrowser2 `/download/jsondata` — gerçek SDMX-JSON 2.1, aynı TÜFE serisi (§1.2) | `3166874dd9d031a83f0e6f94d8ca7c79545b6225331d514955a2f7344a09738c` |
| `siniflama-isic-0111.raw.json` | 496 byte | Sınıflama Sunucusu, hedefli tek-kod sorgusu — `ust_kod: null` davranışının kanıtı (§1.8) | `e12d556a3ef85bc013053de6aef608bb8667bc68fe4b56e6b80d286117ff8990` |
| `siniflama-isic198-tree-subset.raw.json` | 7.259 byte | Sınıflama Sunucusu, tam ağaç yanıtının ilk 15 satırı (766 satırlık gerçek yanıttan kırpıldı, `ust_kod` dolu) (§1.8) | `7d5d1b752774523c3c176ce9d042e2bbdb5d74061b643d4ce5c4a05ce5628341` |
| `yayin-index-page1.raw.html` | 50.809 byte | Yayın Sistemi bootstrap HTML'i — sayfa 1'in gerçek kayıtlarını da içeriyor (§1.9) | `32a21c674d4c02366f893d7bda2b52ea5029ac9078ae42188cb808bcca2975da` |
| `yayin-page2.raw.txt` | 18.147 byte | Yayın Sistemi `zkau` AU sayfalama yanıtı, gerçek sayfa 2 (§1.9) | `f5f2e1e08897e3e3130039009fd9a8b5370664418a6dd8849fa9d18f10a34a38` |
| `secim-2023-milletvekili-genel.raw.html` | 65.885 byte | Seçim Dağıtım Sistemi — gerçek, uçtan uca üretilmiş 2023 milletvekili seçimi sonucu raporu (§1.10) | `d4d7c7a471f058ea8db750699c0c38c3aba936da0c61e504f67bb6a083e37aca` |
| `turizmapp-cikis-report.raw.html` | 47.269 byte | turizmapp — çıkış yapan ziyaretçi raporu, milliyet×eğitim durumu, 2012 (§1.11) | `c73a1446cc7b097de417dc7dd377da2e8b36e173f10c52da34a32da2f250c825` |
| `turizmapp-giris-report.raw.html` | 95.238 byte | turizmapp — giriş yapan vatandaş raporu, ikamet ili×eğitim durumu, 2012 (§1.11) | `30cb83d9bbaa2e6839dde0ec1e138cc7fd28b85b65c64d0b28d7003c1a8c8db0` |
| `turizmapp-sinir-report.raw.html` | 582.730 byte | turizmapp — yıllık giriş yapan yabancı ziyaretçi raporu, kapı×yol kırılımı, 2025 (§1.11) | `fa42e9ffbbf543bbcedb9f1724c019a11e63af1393b504e79a55018ea9b156e6` |
| `bi-toplam-ihracat-ithalat-2025.raw.xlsx` | 3.197 byte | bi.tuik.gov.tr (Qlik) — 2025 toplam ihracat/ithalat gerçek Excel export'u (§1.12) | `0bb3249094aec74e747a453905f236d9092b0d2b480e342ab77c8e63009764c0` |
| `cip-medas-nufus-duzey3.raw.json` | 6.392 byte | CİP proxy (`kaynak=medas`) — gerçek il nüfus verisi, 81 il × 5 yıl (§1.6) | `115c57c05bf573415dece907bb1c1c5c8b6d74d39384de687dfca1fbb2fd3166` |

- Elde ediliş yöntemi: her biri doğrudan, izole `GET`/`POST` isteğiyle
  (token'sız veya Bearer token'lı, ilgili §1.x alt bölümünde belirtildiği
  gibi) çekildi; hiçbiri elle düzenlenmedi (RULES.md §6).
  `siniflama-isic198-tree-subset.raw.json` istisna olarak **kırpıldı**
  (766 satırlık gerçek yanıttan ilk 15 satır alındı, satır içerikleri
  değiştirilmedi) — tam yanıt `KARVEN-ARAŞTIRMA/tuik/evidence/raw/
  2026-09-14-siniflama-isic198-seviye1-tree.json`'da saklanıyor
  (SHA-256: `776d8454b04ae655eba8072d5bba7707f63e91f30625eb82bbe75f18f4b8f47a`).
- Excel export (§1.7) için ayrı bir fixture taşınmadı — dosya boyutu küçük
  olsa da (15.485 byte) `layout` body'sinin dataflow'a özgü/kırılgan
  olması nedeniyle temsil edici bir "genel" fixture değil; gerekirse ayrı
  bir connector testi aşamasında eklenebilir.

## 4. Ortak modele dönüşüm eşlemesi

| Kaynak alanı | Hedef model | Not |
|---|---|---|
| Kurum sabiti "TÜİK" | `institutions` | Tek kayıt; IMF SDDS/DSBB üyeliği güven düzeyi alanı olarak eklenebilir |
| `INDICATOR` + `SINIFLAMA_DUZEYI`/`COICOP_2018` kombinasyonu | `indicator_definitions` | Her (gösterge, sınıflama düzeyi) kombinasyonu ayrı tanım; COICOP hiyerarşisi `COMPONENT_OF` ilişkisine kaynak |
| `REF_AREA` + `FREQ` + `INDICATOR` + `TIME_PERIOD` dizisi | `data_series` | Aylık/yıllık/çeyreklik varyantlar ayrı seri; İBBS3 bölgesel seriler de ayrı (81 il × gösterge) |
| `OBS_VALUE` + `YAYIM_DONEMI`/`BASE_PER` | `data_vintages` | **Sınırlama:** çoğu dataflow'da yalnızca GÜNCEL vintage sorgulanabiliyor (§5) — gerçek vintage geçmişi API'den gelmiyor |

- **Birim/frekans normalizasyonu:** `UNIT_MEASURE`/`UNIT_MULT` attribute'ları
  değeri çarpan olarak etkiler (örn. `UNIT_MULT=3` → bin katı); connector
  ham `OBS_VALUE`'yu bu attribute'larla birlikte normalize etmeli.
- **Eksik/gizli/geçici değer işaretleme:** Gerçek sıfır ile
  gizlilik-nedeniyle-bastırılmış değer API seviyesinde ayırt edilemiyor
  (CİP proxy'de doğrulandı, §1.6); `CONF_STATUS`/`OBS_STATUS`
  attribute'ları varsa kullanılmalı, yoksa eksik gözlem birimi **`null`**
  olarak işaretlenmeli, asla `0` ile karıştırılmamalı.
- **`COMPONENT_OF` hiyerarşisi:** Doğrulanmış kaynak, `siniflama.tuik.gov.tr`
  Sınıflama Sunucusu'nun JSON API'sidir (§1.8) — ISIC Rev.2-5 ve İBBS 2005
  için `kod`/`ust_kod` alanlarıyla gerçek ağaç yürüyüşüyle doğrulandı (A).
  COICOP-2018 (TÜFE alt grupları) hiyerarşisinin AYNI serviste bulunup
  bulunmadığı ayrıca test edilmedi (D seviyesi) — `SINIFLAMA_DUZEYI`/
  `COICOP_2018` alanları nsiws/databrowser2 gözlem yanıtlarında kod olarak
  geçiyor ama bu kodların üst-alt ilişkisini döndüren ayrı bir endpoint
  bu araştırmada doğrulanmadı; connector, TÜFE COICOP hiyerarşisi için
  Sınıflama Sunucusu'nda ayrı bir `surumId` aramalı veya bu alanı D
  seviyesinde bırakmalı.

## 5. Güncelleme / revizyon davranışı

- **Yayın takvimi:** Basın bülteni sistemi üzerinden gerçek, planlı yayım
  tarihleri mevcut — her bülten için önceden ilan edilen tarih/saat.
  (Düzeltme: `Dng2026_tr.pdf`, PDF içeriği fiilen okunduğunda — §1.7 —
  bir yayın takvimi değil, "2026 Dünya Nüfus Günü" nüfus istatistikleri
  infografiği olduğu görüldü; format doğrulaması için kullanıldı, yayın
  takvimi kanıtı olarak kullanılmamalı.)
- **Revizyon/vintage sorgulanabilirliği — ÖNEMLİ SINIRLAMA:** En az iki
  bağımsız dataflow ailesinde (`DF_YAPISAL_EFGTG_BUYUKLUK_NACEREV21`'in
  `YAYIM_DONEMI`, `UH_BH_GSYH_HACIM`'in `BAZ_YILI`) DSD'de revizyon/baz-yılı
  boyutu TANIMLI olsa da, gerçek API'de yalnızca **tek bir güncel değer
  seçilebilir durumda** (`isSelectable:true` olan tek kod) — geçmiş
  vintage'lara API üzerinden geçiş yapılamıyor. Bu **B+ seviyesinde bir
  genelleme** (iki bağımsız A-seviyeli gözlemle desteklenen bir örüntü);
  tüm 421+ dataflow için garanti edilmiyor.
- **TÜFE baz yılı/rebase davranışı (A seviyesi, gerçek dipnot metniyle
  doğrulandı):** 2026 itibarıyla TÜFE 2025=100 baza geçti; TÜİK **geriye
  dönük tüm seriyi yeniden hesaplıyor** ve COICOP-2018 sınıflamasına göre
  yeniden düzenliyor. Sorgulanan seri her zaman en güncel baza normalize
  edilmiş geliyor — eski-baz ham değerlere ayrı erişim yok (en azından
  varsayılan görünümde; API üzerinden eski `BASE_PER` sorgulanabilir mi
  test edilmedi, D seviyesi).
- **Geçmiş veri kapsamı:** TÜFE serisi fiilen 2005-01'e kadar test edildi
  (A). Seçim istatistikleri 1950'ye kadar test edildi (A, ayrı sistem).
- **Vintage ayrımı sonucu:** TÜİK bu servisler üzerinden "ilk yayım vs.
  revize değer" ayrımını sunmuyor — yalnızca güncel, nihai değer erişilebilir.

## 6. Erişim / hata senaryoları (RULES.md §8, fiilen test edilenler)

| Senaryo | Denendi mi | Gerçek sonuç |
|---|---|---|
| Geçersiz seri/gösterge | Evet | nsiws: 404 "Could not find Dataflow and/or DSD..."; databrowser2: 500 `DATAFLOW_NOT_FOUND` (çoğunlukla yanlış sürüm numarasından kaynaklanıyor, doğru sürümle 200) |
| Eksik parametre | Evet | `startPeriod`/`endPeriod` her ikisi de opsiyonel — atlanınca tam seri döner (200); boş seri anahtarı da 200 dönebiliyor, çoklu seri döndürebilir |
| Boş sonuç | Evet | databrowser2: `{"id":[],"size":[],"value":{}}` (200, hata değil) — genellikle varsayılan `REF_AREA=TR` o DSD'de seçilebilir değilse |
| Hatalı tarih/filtre | Evet | nsiws: malformed `startPeriod` (örn. `abc`) → **422** "Semantic Error - Invalid Date Format" (404'ten ayrı, net kod — istemci tekrar denememeli, isteği düzeltmeli); ters tarih aralığı (start>end) → 404 "NoRecordsFound" |
| Aynı isteğin tekrarı | Evet | Rate-limit YOK ama ~%25 sessiz timeout paterni sabit (§1.3) |
| Rate limit / throttling | Evet | Açık 429 hiç görülmedi; sunucu tarafı yük-dengeleme paterniyle tutarlı sessiz timeout (~%25, istek sırasına göre deterministik, zamana göre değil) |
| Geçici hata / tekrar deneme | Evet | 1-2 tekrar deneme üretimde güvenle kullanılabilir (15dk boyunca oran hiç bozulmadı) |
| Kimlik doğrulama olmadan istek | Evet | nsiws: 401 "Unauthorized" (49ms, hızlı/net); databrowser2: kimlik doğrulama gerektirmiyor (tasarım gereği) |

Test edilmeyen senaryo "desteklenmiyor" değil, **"test edilmedi"** olarak
işaretlenir (örn. `DF_GENEL_DEVLET_ANA_BILESENLERI_CEYREKLIK`'in kalıcı
tutarsız davranışı hâlâ kesin sonuca bağlanamadı, §10).

## 7. Kaynak güven rolü

- TÜİK, KARVEN'de **birincil** kaynak rolünde — Türkiye'nin resmi istatistik
  otoritesi, tek kurumsal alternatifi yok (TCMB farklı bir kurum ve bu
  araştırmanın kapsamı dışında bırakıldı).
- **Somut otorite göstergesi:** `Turcat` sayfasındaki her satır IMF DSBB'ye
  (`dsbb.imf.org/sdds/dqaf-base/country/TUR/...`) doğrudan bağlı — TÜİK
  resmi olarak IMF SDDS'e (Special Data Dissemination Standard) veri
  raporluyor. Bu, en üst düzeyde uluslararası doğrulanmış veri kalitesi
  göstergesidir.
- **Çapraz doğrulama:** Aynı TÜFE endeks değerinin nsiws (Bearer token'lı),
  databrowser2 (token'sız) ve export/CSV endpoint'i (token'sız) üzerinden
  BAĞIMSIZ olarak aynı sayısal değeri döndürdüğü doğrulandı — üç farklı
  teknik yoldan aynı veri, iç tutarlılık kanıtı.

## 8. Connector tasarım kararı

- **Önerilen birincil çekim yöntemi:** `databrowser2.tuik.gov.tr`'nin
  token'sız JSON-stat/export API'si (doğrudan HTTP, headless browser
  GEREKMİYOR — düz `POST` isteği).
- **Veri Portalı (basın bülteni, toplu indirme) için:** düz HTTP istemcisi;
  her istekte Chrome `User-Agent` + `X-Requested-With: XMLHttpRequest`.
  Dosya indirmelerinde istekler arası ≥5 sn (§1.5, 02.10.2026 ölçümü).
- **Kimlik doğrulama otomasyonu (yalnızca nsiws'e düşülürse gerekli):**
  Keycloak token endpoint'inden JWT alınmalı, ~5 dakikalık ömür nedeniyle
  her istekte veya kısa aralıkla yenilenmeli.
- **Rate limit'e saygılı çekim aralığı önerisi:** Sabit ~%25 sessiz-timeout
  oranı zamana göre kötüleşmiyor; 1-2 tekrar-deneme + istek başına en az
  birkaç yüz ms bekleme yeterli görünüyor (4s aralıkla test edilen 15dk'lık
  koşuda oran değişmedi).
- **Otomasyon stratejisi gerektiren kısımlar:** Basın bülteni ID'leri
  tahmin edilemez/artımlı olmayabilir (yalnızca örnek-güven taraması
  yapıldı, tam arşiv taraması KASITLI olarak yapılmadı — kullanıcı talebi).
- **Bilinen sınırlamalar:**
  - Vintage/geçmiş revizyon sorgulanamıyor (§5) — connector yalnızca güncel
    değeri çekebilir, "ilk yayım" değerini asla elde edemez.
  - `duzey=5` (CİP) hiçbir zaman veri döndürmüyor — desteklenmeyecek.
  - `DF_GENEL_DEVLET_ANA_BILESENLERI_CEYREKLIK` tutarsız/kararsız —
    connector bu dataflow için uzun timeout + graceful-skip mantığı
    içermeli, "veri yok" olarak yorumlamamalı.
  - TÜİK Mikro Veri Seti (`.sav`/`.csv`/`.spss` ham dosyalar) desteklenmiyor
    — gerçek kurumsal başvuru süreci gerektiriyor, otomasyona uygun değil.
- **Format önceliği (§1.7'deki A-seviyeli doğrulamaya göre):**
  1. XML/JSON/CSV (databrowser2 export) — sabit, dataflow-bağımsız istek
     şablonu; en düşük entegrasyon maliyeti.
  2. Excel export — çalışıyor (A) ama her dataflow için doğru `layout`/
     `filtersValue` body'sinin türetilmesi ek mühendislik gerektiriyor;
     yalnızca kullanıcı-tarafı "dosya indir" özelliği için önerilir, toplu
     otomatik çekim için değil.
  3. PDF çıkarımı — çalışıyor (A) ama serbest metin/infografik düzeninde;
     yapılandırılmış veri için düzen-bazlı ayrıştırma gerektirir, yalnızca
     API'de bulunmayan içerik (bülten yorumu/anlatı metni) için düşünülmeli.

## 9. Test senaryoları (connector test suite'i için)

- **Normal senaryo fixture'ı:** Turcat `ViewTurcatPageNufus` yanıtı (küçük,
  temiz, deterministik).
- **Şema-şüphesi alarmı:** DSD boyut listesi (`id` array'i) değişirse veya
  `OBS_VALUE`/attribute alan adları değişirse tetiklenmeli — bu alanlar
  kod boyunca sabit varsayılmamalı.
- **Hata senaryosu fixture'ları:** 401 (`response-401-dataflow.raw.txt`),
  403 (`response-403-portal-search.raw.txt`) ham dosyaları mevcut,
  taşınabilir.
- **Boş sonuç senaryosu:** databrowser2 `{"id":[],"size":[],"value":{}}`
  yanıtı — connector'ın bunu hata değil "gerçekten boş" olarak
  yorumladığını doğrulayan test.
- **Mock connector testi kapsamı:** 200/401/404/422/timeout beşlisinin
  hepsi ayrı davranış üretmeli (422 asla retry tetiklememeli, timeout 1-2
  retry tetiklemeli, 401 token yenileme tetiklemeli).
- **Format-çeşitliliği fixture'ları:** `evidence/format-testleri/Dng2026_tr.pdf`
  (gerçek PDF, metin çıkarımı testi için) ve Excel export'un ZIP/XLSX imza
  doğrulaması (`50 4B 03 04`) — connector'ın "gerçek dosya mı, hata sayfası
  mı" ayrımını doğru yaptığını test etmek için kullanılabilir.
- **Sınıflama hiyerarşisi fixture'ları:** `siniflama-isic-0111.raw.json`
  (hedefli sorgu, `ust_kod: null` — connector'ın bu durumda edge üretmeden
  sessizce geçmesi test edilmeli) ve `siniflama-isic198-tree-subset.raw.json`
  (ağaç yürüyüşü, `ust_kod` dolu — `COMPONENT_OF` edge üretiminin doğru
  çalıştığını doğrulayan asıl test fixture'ı).

## 10. Açık kalan sorular (D seviyesi)

- `DF_GENEL_DEVLET_ANA_BILESENLERI_CEYREKLIK`'in tutarsız davranışının
  (bazen 1sn'de boş 204, bazen 3dk+ hiç yanıt yok) kesin nedeni bulunamadı;
  12 denemelik kalıcı retry testi de kesin sonuca bağlanamadı.
  (`evidence/kalan-114-dataflow-taramasi.md`)
- ~%25 sessiz-timeout paterninin IP bazlı mı, token/kullanıcı bazlı mı,
  yoksa dataflow bazlı mı olduğu ayrıca test edilmedi.
- "İstek sıra numarasına göre round-robin/node havuzu" hipotezi makul bir
  açıklama ama response header'larında node/instance ID kontrol edilmedi.
- 15 dakikadan çok daha uzun (saatler süren) bir yük senaryosu test
  edilmedi.
- Eski baz yılı (`BASE_PER`) değerlerine API üzerinden erişilip
  erişilemediği (TÜFE rebase sonrası) test edilmedi.
- databrowser2'nin serbest tarih aralığı/çoklu seri anahtarı desteğinin
  nsiws ile tam eşdeğer olup olmadığı test edilmedi.
- Basın bülteni ID aralığının tam yapısı/artımlılığı — kasıtlı olarak tam
  arşiv taraması yapılmadı (kullanıcı talebiyle, sadece güven-örneklemi).
- ~~Basın bülteni WAF'ının headless ile geçilip geçilemeyeceği~~ —
  **kapandı (02.10.2026):** tarayıcı gerekmiyor, başlık kontrolü (§1.5).
- `ilgosterge` (il göstergeleri) uygulamasındaki "Düzey, gösterge ve zaman
  listelerinden en az birer kayıt seçili olmalıdır!" validasyon hatasının
  kök nedeni (gizli bir "Düzey" seçici) bulunamadı — hem Codex hem manuel
  denemeler başarısız oldu, RULES §8 eşiği (2-3 deneme) aşıldığında D
  olarak bırakıldı.
- TÜİK Mikro Veri Seti'nin gerçek dosya formatları/indirme akışı hiç
  denenmedi — kurumsal başvuru süreci gerektiriyor, bu araştırmanın
  yetki kapsamı dışında (RULES.md §10).
- `bi.tuik.gov.tr`/`report-bi.tuik.gov.tr` (Qlik BI, dış ticaret) export
  dışındaki etkileşimli rapor endpoint'leri yalnızca kısmen probe edildi.
- **Excel export'un `layout` body'sinin otomatik türetilebilirliği test
  edilmedi.** TÜFE için gerçek layout body'si yakalandı (§1.7), ama bu
  body'nin başka bir dataflow için nasıl otomatik oluşturulacağı (DSD'den
  mi türetilecek, yoksa her dataflow için manuel mi çıkarılacak) test
  edilmedi — Excel'in genel/ölçeklenebilir bir çekim yöntemi olup olmadığı
  hâlâ açık.
- PDF içindeki verinin yapılandırılmış (tablo) çıkarımı için düzen-bazlı
  bir ayrıştırıcının ne kadar güvenilir çalışacağı test edilmedi — yalnızca
  serbest metin çıkarımının çalıştığı (ham metin okunabilir olduğu)
  doğrulandı, sayısal değerlerin doğru etiketle eşleştirilmesi (örn. "1,42"
  değerinin "toplam doğurganlık hızı"na ait olduğunun otomatik çıkarımı)
  ayrıca test edilmedi.

## 11. Bilinen dataflow envanteri (Ek)

- TÜİK'in nsiws SDMX kataloğunda (`GET https://nsiws.tuik.gov.tr/rest/dataflow/TR/all/latest?detail=full`,
  A seviyesi) **421 dataflow ve 55 farklı DSD** tespit edildi — bu, TÜİK'in
  bu servis üzerinden sunduğu göstergelerin (TÜFE, GSYH, işsizlik, göç,
  tarım, sanayi, dış ticaret, eğitim vb.) tam listesidir.
- Tam katalog `KARVEN-ARAŞTIRMA\tuik\catalog\dataflow-index.csv` (421
  satır + başlık, 81,8 KB) ve ham XML yanıtı
  `KARVEN-ARAŞTIRMA\tuik\catalog\dataflows-latest.xml` (444,9 KB) olarak
  duruyor — bu profile kopyalanmadı (boyut nedeniyle), ama **hiçbiri
  kayıp değil**: connector Task 1.5'te yazılırken bu katalog doğrudan
  kaynak olarak kullanılabilir.
- Bu 421 dataflow'un **360'ından fazlası** için gerçek, indirilmiş örnek
  veri zaten mevcut (110 + 7 + 243 = 360 doğrulanmış dataflow×bölge
  kombinasyonu, bkz. §1.2, `evidence/kalan-114-dataflow-taramasi.md`,
  `evidence/hatali-kayitlarin-gercek-durumu.md`) — bu ham veriler
  `KARVEN-ARAŞTIRMA\tuik\data\` altındaki ilgili klasörlerde duruyor.
- **Sonuç:** Bu profil her dataflow'un alan yapısını tek tek dökmese de,
  (a) dataflow'ların TAM listesi biliniyor ve dosya olarak mevcut, (b)
  bunların büyük çoğunluğu için erişim yöntemi zaten A seviyesinde
  doğrulanmış ve örnek veri zaten çekilmiş durumda. Connector (Task 1.5),
  TÜFE için kanıtlanan aynı genel yöntemi (databrowser2 JSON-stat/export)
  bu kataloğun tamamına programatik olarak uygulayabilir.

---

*Bu dosya doldurulduktan sonra, ham araştırma klasörüne
(`KARVEN-ARAŞTIRMA\tuik\`) bir daha dönülmeyecek şekilde tasarlanmıştır.
Sadece bu dosya (+ gerekirse 1-2 küçük örnek veri dosyası, bkz. §3)
`KARVEN\docs\kaynak-profilleri\tuik.md` olarak ana projeye taşınacaktır —
bu taşıma kullanıcı onayı gerektirir (RULES.md §11) ve henüz yapılmamıştır.*
## Güncelleme: COICOP-2018 canlı hiyerarşi kanıtı

`siniflama.tuik.gov.tr` COICOP-2018 `surumId=1332` ile canlı doğrulandı;
resmi ağaç isteği 587 `kod -> ust_kod` kenarı döndürdü. Bu resmi kenarlar
`COMPONENT_OF` projeksiyonunun kaynağıdır. Genel endeks için `DEGISIM=1`
endeks düzeyi tam dolu 260 aylık gözlem sağlarken değişim oranı
varyantları seyrektir; dönem sayısı dolu değer sayısı değildir.
## Correction note (2026-09-21)

The live COICOP-2018 classification endpoint was validated with surumId=1332 and 587 kod-to-ust_kod edges. For CPI variants, DEGISIM=1 (index level) is fully populated for 260 periods, while change-rate variants are sparse; period counts must not be mistaken for non-null value counts.
