# Kaynak Profili: Türkiye Cumhuriyet Merkez Bankası (TCMB)

- Araştırma klasörü: `KARVEN-ARAŞTIRMA\tcmb\`
- Araştırma dönemi: 16.09.2026–18.09.2026
- Profil tarihi: 18.09.2026
- Profili dolduran: Codex araştırma oturumu
- Durum: Uygulama doğrulandı — EVDS3, tamamlayıcı kanallar, operasyonel bağlama
  ve kontrollü PDF batch akışı hazır; canlı Compose doğrulaması ortam bağımlıdır

> Bu profil, TCMB araştırma klasöründeki gerçek istek/yanıt kanıtlarının
> özetidir. PDF arşivinin belge türünü anlamak için 30 temsili belge
> incelenmiştir; tamamının indirilmesi ve işlenmesi ileri faz kapsamındadır.

## 0. Yönetici özeti

TCMB için üretime alınacak ana kanal EVDS3'tür. Katalogdan seri keşfi yapılır,
seri bounds bilgisi alınır ve gerçek gözlemler `/fe` endpoint'inden JSON olarak
çekilir. Bu akış key'siz çalışırken, verilen `key` header'ı ile yapılan canlı
karşılaştırmada response farkı görülmemiştir; key yine de güvenli yapılandırma
alanı olarak opsiyonel tutulmalıdır.

Doğrulanan kapsam:

1. 243 saf TCMB grubu ve 27.272 seri katalogdan keşfedildi.
2. Tüm 27.272 seri 398 çoklu-seri batch'inde bounds/FE sözleşmesi açısından
   doğrulandı; 42.066 FE satırı yeniden kontrol edildi.
3. Günlük/saatlik kur XML'leri, ana sayfa oran JSON'u, duyurular, dashboard'lar,
   yayın takvimi ve RSS/Atom feed'leri ayrıca gözlendi.
4. TCMB sitesinde 428 sayfa ve 4.063 PDF URL'si envanterlendi; arşivin türünü
   anlamak için 30 temsili PDF indirildi ve ayrıştırıldı.

Connector geliştirme sırası EVDS3 katalog → seri listesi → bounds → `/fe`;
ardından tamamlayıcı kanallar ve ortak operasyonel bağlama olarak uygulandı.
EVDS2 explicit HTTPS TCMB URL + feature flag sınırında, PDF arşivi ise açık URL
envanteri ve `max_documents` ile kontrollü batch olarak çalışır.

### Planlanan fazlar

| Faz | Kapsam | Çıkış ölçütü |
|---|---|---|
| 1 | EVDS3 ana veri akışı | 27.272 seri sözleşmesi, parser ve temel connector testleri |
| 2 | Kur, takvim, duyuru ve RSS | Her kanalın ayrı kaynak/document modeli ve fixture'ı |
| 3 | EVDS2 legacy auth/uyumluluk | Explicit HTTPS TCMB URL, query credential reddi, feature flag ve güvenli auth akışı |
| 4 | Dayanıklılık | Ölçümlü retry/backoff, timeout, 429/5xx sınıflandırması ve istemci sayaçları |
| 5 | Vintage/revizyon | Aynı seri/dönem için ilk ve revize değerleri ayıran doğrulanmış kaynak akışı |
| 6 | PDF arşivi | Açık URL envanterinden sınırlı batch indirme, metin/metadata çıkarma ve belge modeli |

## 1. Kaynak teknik profili

### 1.1 EVDS3 katalog ve seri keşfi

```text
İddia: EVDS3, TCMB serilerinin key'siz katalog ve seri keşif kanalını sunar.
Kaynak sayfa: https://evds3.tcmb.gov.tr/
Araştırma tarihi: 16–17.09.2026
İstek yöntemi: GET
İstek URL'si: https://evds3.tcmb.gov.tr/igmevdsms-dis/categories/withDatagroups/type=json
Gerekli header/cookie: Testte gerekli değildi.
HTTP durum kodu: 200
Response content-type: JSON
Response alanları: kategori, data group, DATAGROUP_CODE, DATASOURCE,
  frekans, birim, LAST_UPDATED ve metadata/revizyon bağlantıları.
Gerçek sonuç: 154 kategori, 678 grup, 243 saf TCMB grubu.
Kanıt dosyaları: tcmb/evidence/dis-katalog-ve-seri-kesfi.md,
  tcmb/evidence/seri-listesi-tam-matrisi.md,
  tcmb/evidence/parsed/2026-09-16-cbrt-group-series-probe.json
Kanıt seviyesi: A
Sonuç: Connector, kullanıcı adı yerine katalogdan DATAGROUP_CODE ve SERIE_CODE keşfetmelidir.
```

Seri listesi: `GET /igmevdsms-dis/serieList/fe/type=json&code={DATAGROUP_CODE}`.
Geçerli gruplarda 200 JSON döner; seri kodu, ad, frekans ve aggregation yetenekleri
bulunur. `code=undefined` için 200 ve boş liste görüldü; HTTP 200 tek başına veri
varlığı anlamına gelmez.

### 1.2 EVDS3 bounds ve veri

```text
İddia: Katalogdan keşfedilen seri için tarih sınırı ve gözlemler alınabilir.
İstek URL'leri:
  POST https://evds3.tcmb.gov.tr/igmevdsms-dis/serieList/baslangicBitis
  POST https://evds3.tcmb.gov.tr/igmevdsms-dis/fe
Request body: JSON; seri, aggregationTypes, formulas, startDate, endDate,
  frequency, decimalSeperator, decimal, dateFormat, lang, yon, sira ve UI
  uyumluluk alanları.
Gerekli header/cookie: Testte key/cookie gerekli değildi.
HTTP durum kodu: Başarılı sorguda 200 JSON.
Bounds alanları: startDate, endDate, maxStartDate, minEndDate, frequency.
FE alanları: totalCount, items, Tarih, seri kolonları, UNIXTIME.
Örnek seri: TP.DK.USD.A.EF.YTL; örnek değer 48.5245.
Kanıt dosyaları: tcmb/evidence/dis-veri-sorgu-akisi.md,
  tcmb/evidence/cbrt-all-series-coverage.md,
  tcmb/evidence/parsed/2026-09-17-cbrt-all-series.json
Kanıt seviyesi: A
```

Geçerli örnek body:

```json
{"type":"json","series":"TP.DK.USD.A.EF.YTL","aggregationTypes":"avg","formulas":"0","startDate":"16-09-2026","endDate":"16-09-2026","frequency":"1","decimalSeperator":".","decimal":"4","dateFormat":"0","lang":"tr","yon":"0","sira":"0","ozelFormuller":[],"groupSeperator":true,"isRaporSayfasi":false}
```

### 1.3 Tam seri kapsamı

243 saf TCMB grubu, 27.272 seri ve 398 batch test edildi. 796 canonical
response başarılı oldu; beklenen seri kolonu eksik batch yoktur. Bu, sorgu
sözleşmesini doğrular; her serinin her tarihinin dolu olacağı anlamına gelmez.

## 2. Tamamlayıcı TCMB kanalları

| Kanal | İstek | Doğrulanan sonuç | Connector kararı |
|---|---|---|---|
| Günlük kur | `GET https://www.tcmb.gov.tr/kurlar/today.xml` | 200 XML; Currency, ForexBuying/Selling, Banknote alanları | Ayrı kur adapter’ı |
| Saatlik reeskont | `GET https://www.tcmb.gov.tr/reeskontkur/...xml` | 200 XML örnekleri | Ayrı adapter, kur serisiyle karıştırma |
| Ana sayfa oranları | `GET .../anasayfa_faizorani.json` | 200; body JSON, header HTML | Tamamlayıcı snapshot |
| EVDS3 duyuruları | `GET .../announcements` | 200 JSON; 223 kayıt görünümü | İsteğe bağlı doküman adapter’ı |
| EVDS3 takvimi | `GET .../calendar/aylikYayinlar?yil=2026&ay=9` | 200 JSON; 104 öğe örneği | Yayın planı adapter’ı |
| Appg takvimi | `GET https://appg.tcmb.gov.tr/igmvytsms-dis/veriTakvimi/...` | Sayfalı 200 JSON | Ayrı adapter |
| RSS | TCMB RSS yolları | Body Atom XML, header bazen HTML | Content-Type’a kör güvenme |

## 3. Veri sözlüğü ve dönüşüm

| Kaynak alanı | Anlamı | Ortak modele öneri |
|---|---|---|
| `DATAGROUP_CODE` | Veri grubu kimliği | `indicator_definitions.source_group_code` |
| `SERIE_CODE` | Seri kimliği | `data_series.external_id` |
| Seri adı | Göstergenin açıklaması | `indicator_definitions.name` |
| `frequency` | Bounds/FE sorgu frekansı | `data_series.frequency` |
| `BIRIMI` / `BIRIMI_EN` | Birim | `data_series.unit` |
| `Tarih` | Gözlem tarihi/dönemi | `period` |
| Seri kolonu | Gözlem değeri; çoğunlukla string | `value`, locale-aware parse |
| `UNIXTIME.$numberLong` | Kaynak zaman bilgisi | `attributes.source_unixtime` |
| `null` | Eksik gözlem | `status=missing`; asla 0 yapma |
| `LAST_UPDATED` | Metadata güncelleme sinyali | `attributes.source_last_updated` |

Katalogdaki frekans metadata kodları ile bounds/FE request frekans kodları
doğrudan eşlenmemelidir; endpoint response'undaki gerçek `frequency` değeri
FE isteğine taşınmalıdır. Aggregation yalnızca seri yeteneklerinde destekleniyorsa
gönderilmelidir.

## 4. Güncelleme / revizyon davranışı

- 16–17.09.2026 katalog snapshot'larında 154 kategori, 678 grup ve grup
  metadata'sı değişmedi.
- 60 geçerli FE isteği 250 ms aralıkla 60/60 HTTP 200 döndürdü; resmi burst
  kotası ve 429 sözleşmesi kesinleştirilmedi.
- EVDS3 ARCHIVE ağacında `bie_cli / TP.CLI.A01` için 2001–2014 arşiv verisi
  alındı. Bu ayrı/eski seri ailesidir; aynı serinin vintage geçmişi değildir.
- Genel tarihsel revision-history API'si veya aynı tarihe ait iki vintage
  response'u kanıtlanmadı.

## 5. Erişim ve hata senaryoları

| Senaryo | Gerçek sonuç |
|---|---|
| Geçerli FE | 200 JSON |
| Geçersiz seri | 500 HTML örneği |
| Hatalı tarih | 500 HTML |
| Eksik body | 400 HTML |
| Bounds eksik body | 400 HTML |
| Geçersiz bounds seri | 200 JSON, null sınırlar |
| Ters tarih aralığı | 200 JSON, `totalCount=0`, boş items |
| Aynı istek tekrarı | 3/3 200, aynı response hash'i |
| Key'siz FE | 200 JSON |
| `key` header'lı FE | Key'siz response ile aynı hash |
| EVDS2 legacy | 302 ile EVDS3'e yönlendirme; 401/403 görülmedi |
| Rate-limit | 60/60 kontrollü istek 200; resmi eşik test edilmedi |

## 6. Connector tasarım kararı

1. Doğrudan HTTP adapter kullan; headless browser gerekmez. PDF ve bazı
   legacy/auth akışlarında browser gereksinimi çıkarsa ayrı adapter olarak
   izole et.
2. Keşif sırasını katalog → seri listesi → bounds → FE olarak sabitle.
3. API key'i secret/config üzerinden oku; response içine veya loglara yazma.
   EVDS3 için testte zorunlu olmadığı kanıtlandı, ancak EVDS2 uyumluluk fazı
   için aynı secret altyapısı zorunlu tasarım girdisidir.
4. 429, 5xx, timeout ve HTML error body'lerini ayrı sınıflandır; bounded
   exponential backoff uygula. 200 + boş JSON'u hata gibi yorumlama.
5. String sayıları locale-aware parse et; ham değeri koru; `null` değerleri
   eksik olarak sakla.
6. Katalogda `DATASOURCE_ENG == CBRT` ile saf TCMB gruplarını ayır; karma
   grupları yanlışlıkla TCMB kaynağı olarak etiketleme.
7. Kur XML, takvim, PDF ve duyuru belgelerini EVDS gözlemlerinden ayrı kaynak
   türleri olarak modelle; PDF içeriklerini gözlem serisi gibi yazma.
8. EVDS2 için redirect, auth ve response uyumluluğunu feature flag/adapter
   sınırında tut; EVDS3 ana akışını legacy belirsizliğine bağlama.
9. Vintage/revizyon kayıtlarını yeni snapshot olarak sakla; doğrulanmış
   revision id/vintage anahtarı bulunmadan iki değeri aynı seri hücresinde
   sessizce ezme.
10. PDF arşivinde envanter → önceliklendirme → kontrollü indirme → hash,
    metadata ve metin çıkarma adımlarını ayrı iş akışları olarak uygula.

### 6.1 Uygulanan kontrollü PDF batch sözleşmesi

Celery `karven.tcmb.connector_run` görevi `kind=pdf_batch` ile açık bir `urls`
listesi ve `max_documents` sınırı kabul eder. Her URL deterministik dış kimlik
ile ayrı `pdf` kaynağına dönüştürülür; mevcut snapshot, parser, ortak belge
modeli ve idempotent persistence zincirinden geçer. URL listesi verilmeden veya
limit pozitif olmadan arşiv taraması başlatılmaz.

EVDS2 için `TCMB_EVDS2_ENABLED=true` ve `TCMB_EVDS2_URL` birlikte gerekir.
URL yalnızca HTTPS, `*.tcmb.gov.tr` hostu ve query-string içinde credential
olmayan bir endpoint olabilir; API key yine URL'ye değil `key` header'ına eklenir.

## 7. Test senaryoları

- Fixture: USD FE tek gözlem response'u, çoklu-seri batch response'u, bounds
  response'u, geçersiz seri HTML'i, eksik body HTML'i.
- Katalog fixture'ı: 243 grup / 27.272 seri sayımını ve `SERIE_CODE` keşfini
  doğrulamalı.
- Parser: `Tarih`, seri kolonları, `UNIXTIME.$numberLong`, string ondalık,
  negatif değer ve `null` gözlemlerini test etmeli.
- Contract alarmı: beklenmeyen item alanı veya beklenen seri kolonunun
  kaybolması schema-suspicion olarak yükseltilmeli.
- Canlı düşük hacimli probe: katalog endpoint'i, bir bounds ve bir FE isteği;
  tüm 27.272 seriyi her çalıştırmada yeniden çekmemeli.

## 8. İleri faz gereksinimleri ve açık kalan sorular

- EVDS2 redirect sonrası güncel ayrı host/path ve key zorunluluğu canlı olarak
  doğrulanmadı; adapter bu nedenle explicit URL ve feature flag arkasında
  tutuluyor. Gerçek credential davranışı için deployment ortamında acceptance
  testi gerekir.
- Resmi EVDS3 rate-limit kotası ve burst eşiği bilinmiyor. İstemci artık
  request/retry/429/5xx/transport sayaçlarını tutuyor; kota doğrulaması için
  kontrollü canlı ölçüm ve runtime metric exporter ayrıca gerekir.
- Katalog frekans kodlarının tamamı ile request/response frekans kodlarının
  eksiksiz eşlemesi bilinmiyor.
- Aynı seri için tarihsel vintage/revizyon geçmişi endpoint'i kanıtlanmadı.
- Arşivdeki her kategori için ayrı semantik davranış tek tek test edilmedi.
- PDF arşivinin tüm belgeleri otomatik indirilmeyecek. Faz 6'nın güvenli
  teslimatı açık URL listeli ve sınırlı `pdf_batch` akışıdır; önceliklendirme
  kuralları ve harici envanter kaynağı ürün ihtiyacına göre ayrıca seçilebilir.

---

Ana araştırma kaydı: `KARVEN-ARAŞTIRMA\tcmb\01-ilk-bulgular.md`  
PDF örneklem kaydı: `KARVEN-ARAŞTIRMA\tcmb\evidence\tcmb-pdf-sample-30-2026-09-18.md`
