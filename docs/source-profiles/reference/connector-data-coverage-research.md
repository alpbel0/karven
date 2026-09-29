# Connector Veri Kapsamı Araştırması

**Tarih:** 2026-09-20  
**Kapsam:** Yalnız yerel `KARVEN` ve `KARVEN-ARAŞTIRMA` dosyaları. Bu çalışma sırasında ağ isteği, veritabanı yazımı, kod değişikliği ve git işlemi yapılmadı. `KARVEN-ARAŞTIRMA\secrets\` açılmadı.

## Kanıt standardı

Arşiv `RULES.md` A/B/C/D standardını kullanıyor. Bu raporda:

- **Doğrulandı (A):** Arşivde gerçek istek/yanıt veya ham veri dosyası var.
- **Muhtemel:** Yerel metadata/XML güçlü aday gösteriyor, fakat istenen kapsamda bağımsız veri hesabı yok.
- **Bilinmiyor (D):** Arşivde yeterli kanıt yok.

Dosya adlarından veya katalog başlığından çıkarılan sonuçlar gerçek veri yanıtı yerine geçirilmedi.

## 1. TÜFE alt grupları

### Kısa cevap

**Evet, ayrı dataflow'lar var ve arşivde aylık alt grup verisi bulunuyor.** Katalogdaki ilgili tüm eşleşmeler:

| Dataflow | Katalog satırı | Başlık | DSD |
|---|---:|---|---|
| `DF_TUFE_SDMX_TT01` | `tuik/catalog/dataflow-index.csv:367` | Ana harcama gruplarına göre TÜFE ve değişim oranları | `DSD_TUFE` 1.12 |
| `DF_TUFE_SDMX_TT02` | `.../dataflow-index.csv:368` | COICOP 2018 alt sınıflarına göre aylık/yıllık değişim | `DSD_TUFE` 1.12 |
| `DF_TUFE_SDMX_TT03` | `.../dataflow-index.csv:369` | Ana grupların değişimi ve katkısı | `DSD_TUFE` 1.12 |
| `DF_TUFE_SDMX_TT04` | `.../dataflow-index.csv:370` | Mal/hizmet sepeti ve ağırlıklar | `DSD_TUFE` 1.12 |
| `DF_TUFE_SDMX_TT05` | `.../dataflow-index.csv:371` | Mevsim etkisinden arındırılmış TÜFE | `DSD_TUFE` 1.12 |
| `DF_TUFE_SDMX_TT06` | `.../dataflow-index.csv:372` | Özel kapsamlı TÜFE göstergeleri | `DSD_TUFE` 1.12 |
| `DF_TUFE_SDMX_TT07` | `.../dataflow-index.csv:373` | Seçilmiş madde ortalama fiyatları | `DSD_TUFE` 1.12 |
| `DF_TUFE_SDMX_TT08` | `.../dataflow-index.csv:374` | Ana grup ağırlıkları | `DSD_TUFE` 1.12 |
| `DF_TUFE_SDMX_TT09` | `.../dataflow-index.csv:375` | Harcama gruplarına göre endeks sonuçları | `DSD_TUFE` 1.12 |
| `DF_TUFE_SDMX_TT10` | `.../dataflow-index.csv:376` | Genel TÜFE ve değişim oranları | `DSD_TUFE` 1.12 |

### Gerçek XML kapsamı

Yerel tarihsel XML’ler küçük bir XML taramasıyla kontrol edildi; `SeriesKey` sayısı, aylık dönem kümesi ve örnek anahtarlar şöyledir:

| Dosya | Seri | Dönem | Örnek anahtar |
|---|---:|---|---|
| `tuik/data/historical-2000-2024/DF_TUFE_SDMX_TT02_1.0.xml` | 384 | 2005-01–2024-12, 240 ay | `FREQ=M`, `SINIFLAMA_DUZEYI=5`, `DEGISIM=2`, `COICOP_2018=01111`, `INDICATOR=F_TFE` |
| `tuik/data/historical-2000-2024/DF_TUFE_SDMX_TT09_1.0.xml` | 325 | 2005-01–2024-12, 240 ay | `FREQ=M`, `SINIFLAMA_DUZEYI=2`, `DEGISIM=1`, `COICOP_2018=01`, `INDICATOR=F_TFE` |
| `tuik/data/historical-2000-2024/DF_TUFE_SDMX_TT10_1.0.xml` | 5 | 2005-01–2024-12, 240 ay | `SINIFLAMA_DUZEYI=TUFE`, `COICOP_2018=0`, `DEGISIM=1`, `INDICATOR=F_TFE` |

Bu sayımlar ham XML dosyalarındaki `SeriesKey` ve `TIME_PERIOD` alanlarından elde edildi; rapora ham XML yazılmadı.

### Doğru TÜFE sorgu anahtarı

`tuik/evidence/tufe-dsd.md:11-23` gerçek DSD sırasını doğruluyor:

```text
REF_AREA.FREQ.SINIFLAMA_DUZEYI.DEGISIM.OZEL_KAPSAM_TUFE.BASE_PER.
YAYIM_DONEMI.COICOP_1999.COICOP_2018.INDICATOR
```

Gerçek genel-endeks sorgusu `tuik/evidence/tufe-data-query.md:7-29` içinde:

```text
TR.M.TUFE.1._Z.2025.2026_01._Z.0.F_TFE
```

Alt grup için değişmesi gereken parça `COICOP_2018` değeridir; örneğin arşiv XML’sindeki gerçek alt grup örneği `01111` ve ana grup örneği `01`’dir. `SINIFLAMA_DUZEYI`, `DEGISIM`, `BASE_PER`, `YAYIM_DONEMI` ve diğer boyutlar seçilen dataflow/seri ile uyumlu tutulmalıdır. `COICOP_1999` bu örneklerde `_Z`’dir.

Repo’nun mevcut `databrowser-tufe-subgroups` kaynağı `src/backend/app/ingestion/data/tuik/connector.py:510-517` içinde `DF_TUFE_SDMX_TT02` ve `COICOP_2018` listesi kullanıyor. Buna karşılık genel kaynak `:483-489` `DF_TUFE_SDMX_TT10` ve yalnız `REF_AREA=TR` body’si kullanıyor. Repo’nun eski/alternatif genel seri sorgusu `COICOP_2018=0` seçer; bu alt grup değildir.

### Sınır

`tuik/evidence/tufe-completion-tests.md:7-28` COICOP-2018 kod listesinin 797 kod içerdiğini ve `01`–`13` ana gruplarını doğruluyor. Aynı dosya, kod listesindeki bir kodun her dataflow/yayım kombinasyonunda mutlaka seri olduğu anlamına gelmediğini; test edilen bazı `01` sorgularının 404 verdiğini açıkça belirtiyor. Bu nedenle **alt grup dataflow/XML varlığı A**, herhangi bir canlı kombinasyonun otomatik çalışacağı iddiası ise **D/C** düzeyindedir.

**Güven:** Dataflow ve arşiv alt grup kapsamı **doğrulandı (A)**; her olası kod kombinasyonu **bilinmiyor (D)**.

## 2. Yapısal yol için gerçek aday

### Kısa cevap

**Arşivde kullanıcının tam koşullarını birlikte sağlayan aylık/çeyreklik, 60+ gözlemli ve doğrudan ISIC Rev.4 veya NACE Rev.2 faaliyet boyutlu bir SDMX dataflow doğrulanamadı.**

Bulunan yakın adaylar:

1. `DF_YAPISAL_EFGTG_NACEREV21`: `tuik/evidence/hatali-kayitlarin-gercek-durumu.md:15-26` içinde gerçek `databrowser2` JSON-stat yanıtı, `EKONOMIK_FAALIYET_NACE_REV_2_1` boyutu ve 346 faaliyet kategorisi doğrulanıyor. Ancak dataflow yıllık ve yerel XML arşivinde 60+ aylık gözlemli ham dosya yok. Bu nedenle yapısal model adayıdır, istenen aylık/çeyreklik aday değildir.
2. `DF_YAPISAL_EFGTG_IMALAT_TEKNO_NACEREV21`: aynı kanıt dosyası `:66-72` gerçek NACE Rev.2.1 seçimini doğruluyor; kanıt yıllık tek/az dönemli portal görünümüdür.
3. `DF_UFE_SANAYI_V2`, `DF_YIUFE_EDO_V1`, `DF_YDUFE_EDO_V1`, `DF_HUFE_EDO_V1` ve `DF_UFE_INSAAT_V1`: katalog satırları `dataflow-index.csv:214`, `:340-342`, `:381-383`, `:414-416` civarındadır. Yerel XML’lerde aylık ve uzun kapsam vardır; fakat gözlenen boyut adları `URUN_UFE_NACE_CPA`, `FAALIYET_CPA_2008`, `FAALIYET_CPA_2_1` gibi UFE/CPA bileşik boyutlarıdır. Bunların repo’daki `siniflama-isic-rev4` ağacıyla kod-uyumlu ISIC Rev.4 veya NACE Rev.2 faaliyet hiyerarşisi olduğu arşivde doğrulanmadı.

UFE XML’lerinin gerçek yerel örnekleri:

| Dosya | Seri | Dönem | Gözlenen faaliyetle ilgili boyutlar |
|---|---:|---|---|
| `tuik/data/historical-2000-2024/DF_UFE_SANAY__V2_1.0.xml` | 4.890 | 2010-01–2024-12, 180 ay | `URUN_UFE_NACE_CPA`, `FAALIYET_CPA_2008`, `FAALIYET_CPA_2_1` |
| `.../DF_Y_UFE_EDO_V1_1.0.xml` | 1.670 | 2000-01–2024-12, 300 ay | aynı UFE/CPA boyut ailesi |
| `.../DF_UFE__NSAAT_V1_1.0.xml` | 120 | 2015-01–2024-12, 120 ay | `FAALIYET_CPA_2_1=F` örneği |
| `.../DF_HUFE_EDO_V1_1.0.xml` | 1.055 | 2017-01–2024-12, 96 ay | `URUN_UFE_NACE_CPA`, `FAALIYET_CPA_2_1` |

Bu XML’lerde 60+ aylık gözlem **doğrulandı**, ancak kod listesinin `ISIC Rev.4`/`NACE Rev.2` parent-child sözleşmesi ve UFE boyutunun mevcut ISIC ağacına birebir map’i **doğrulanmadı**. `tuik/evidence/siniflama-sunucusu.md:44-79` ise ayrı olarak ISIC Rev.4 `surumId=198` ağacının `KISIM/BÖLÜM/GRUP/SINIF` seviyelerini ve `kod/ust_kod` zincirini doğruluyor. Örnek: `0111 -> 011 -> 01 -> A` (`:70-71`). Bu ağaç mevcut repo kaynağı `siniflama-isic-rev4` ile uyumludur; UFE kodlarının aynı sistem olduğu kanıtlanmamıştır.

**Sonuç:** UFE serileri istatistiksel yol için güçlü aylık adaylardır; yapısal `COMPONENT_OF` yoluna ancak UFE’nin gerçek faaliyet kod listesi ve mevcut `siniflama-isic-rev4` arasında kod eşleşmesi ayrıca A seviyesinde gösterilirse alınmalıdır.

**Güven:** NACE Rev.2.1 yıllık dataflow **doğrulandı (A)**; UFE aylık kapsamı **doğrulandı (A)**; UFE→ISIC/NACE yapısal kod uyumu **bilinmiyor (D)**.

## 3. TÜİK istatistiksel aday çiftleri

Aşağıdakiler aynı kurum içi, aylık ve en az 60 ortak dönemli gerçek arşiv serisi adaylarıdır. Bunlar ilişki sonucu değildir; korelasyon, fark/detrend, pencere ve BH hesabı bu araştırmada çalıştırılmadı.

| Aday çift | Dataflow ve örnek seri anahtarı | Frekans / ortak dönem | Arşiv dosyası |
|---|---|---|---|
| 1 | `TT10`: `COICOP_2018=0`, `DEGISIM=1`, `F_TFE` × `TT02`: `COICOP_2018=01111`, `DEGISIM=2`, `F_TFE` | Aylık; 2005-01–2024-12, 240 | `DF_TUFE_SDMX_TT10_1.0.xml`, `DF_TUFE_SDMX_TT02_1.0.xml` |
| 2 | `TT10` genel `F_TFE` × `TT09`: `COICOP_2018=01`, `DEGISIM=1`, `F_TFE` | Aylık; 2005-01–2024-12, 240 | `DF_TUFE_SDMX_TT10_1.0.xml`, `DF_TUFE_SDMX_TT09_1.0.xml` |
| 3 | `TT10` genel `F_TFE` × `DF_YIUFE_EDO_V1`: `F_YIUFE`, `DEGISIM=1` | Aylık; 2005-01–2024-12, 240 | `DF_TUFE_SDMX_TT10_1.0.xml`, `DF_Y_UFE_EDO_V1_1.0.xml` |
| 4 | `TT10` genel `F_TFE` × `DF_UFE_INSAAT_V1`: `F_IME`, `DEGISIM=1` | Aylık; 2015-01–2024-12, 120 | `DF_TUFE_SDMX_TT10_1.0.xml`, `DF_UFE__NSAAT_V1_1.0.xml` |
| 5 | `DF_YIUFE_EDO_V1` × `DF_HUFE_EDO_V1` | Aylık; 2017-01–2024-12, 96 | `DF_Y_UFE_EDO_V1_1.0.xml`, `DF_HUFE_EDO_V1_1.0.xml` |

İlk üç çift tek aile olarak yeterlidir; daha dürüst bir BH ailesi için 4–5 çift birlikte önerilir. `TT02` ve `TT09` dosya serileri sırasıyla 384 ve 325, `TT10` 5, Yİ-ÜFE 1.670, inşaat maliyet 120 ve HÜFE 1.055 seridir. Bu seri sayıları ve dönemler yukarıdaki XML taramasından; dataflow adları `dataflow-index.csv:367-383,414-416` satırlarından doğrulandı.

**Güven:** Aday dataflow/seri kapsamı **doğrulandı (A)**; aday çiftlerin mekanik istatistiksel kabulü **bilinmiyor (D)**.

## 4. TCMB

### Arşiv var mı?

**Evet.** TCMB arşivinde gerçek katalog, bounds ve FE kanıtı var. `tcmb/evidence/katalog-metadata-profili.md:7-32` 243 saf TCMB grubunu, 86 aylık grubu ve frekans metadata’sını veriyor. `tcmb/evidence/cbrt-all-series-coverage.md:6-18` 27.272 serinin 398 batch’te bounds+FE ile doğrulandığını bildiriyor.

### Aylık 60+ adaylar

`tcmb/01-ilk-bulgular.md:50-66` içinde gerçek FE yanıtlarıyla doğrulanan aylık adaylar:

- `bie_cli2 / TP.CLI2.A01`: aylık, 151 kayıt, 2014-02–2026-08.
- `bie_rkgema / TP.GY1.N2.MA`: aylık, 151 kayıt.
- `bie_kkoisma / TP.KKO.MA`: aylık, 151 kayıt.
- `bie_yiydyul / TP.YI001`: aylık, 151 kayıt.

Bunlar seri kodu tahmini değil, katalogdan seçilip bounds sonrası FE’de dönen gerçek serilerdir. `tcmb/evidence/dis-veri-sorgu-akisi.md:45-72` ayrıca üç aylık ve haftalık örnekleri listeler. TCMB tarafında TÜFE `TP.FG.J0` veya belirli bir politika/faiz serisi için arşivde doğrudan 60+ aylık FE kanıtı bulunamadı; bu adlar aranmış, doğrulanmış seri kodu olarak raporlanmıyor.

### USD/TRY bounds çelişkisi

`TP.DK.USD.A.EF.YTL` için iki farklı kanıt var:

- `tcmb/evidence/dis-usd-sinirlari-ve-rate-limit.md:5-23`: bounds yanıtı `19-04-2026`–`16-09-2026`, yaklaşık 5 ay; frekans `1` (günlük), aggregation `avg`.
- `tcmb/evidence/dis-veri-sorgu-akisi.md:25-43`: aynı seri için doğrudan geniş FE isteği `02-01-1990`–`16-09-2026`, `totalCount=13407`; bu bounds yanıtının tarihsel erişimi tek başına temsil etmediğini gösteriyor.

Bu nedenle canlı connector’da bounds cevabı fiili UI aralığı olarak kullanılmalı; tarihsel backfill için ayrı, kontrollü seri/tarih isteği ve response doğrulaması gerekir. “Bounds neden kısa?” sorusuna arşivde kesin sunucu açıklaması yok; **büyük olasılıkla bounds endpoint’inin güncel/varsayılan UI penceresi** olduğu söylenebilir, fakat bu tahmindir.

TCMB veri isteği için doğrulanan aggregation/frekans örneği `tcmb/evidence/dis-veri-sorgu-akisi.md:12-23` içindeki `aggregationTypes=avg`, `frequency=1` gövdesidir. Aylık seri için sabit kod tahmin edilmemeli; katalog metadata’dan seri ve grup alınmalı, bounds response’un döndürdüğü gerçek `frequency` FE isteğine taşınmalıdır (`katalog-metadata-profili.md:34-40`).

**Güven:** TCMB aylık seri örnekleri **doğrulandı (A)**; `TP.FG.J0` ve faiz kodları **bulunamadı/doğrulanmadı (D)**; USD bounds açıklaması **muhtemel**, kesin kök neden **bilinmiyor**.

## 5. Connector’ların çalışmaması: operasyonel neden

### Koddan doğrulanan akış

- `src/backend/app/infrastructure/celery/beat.py:9-24`: heartbeat 30 saniye; TÜİK canlı probe ve lineage 86.400 saniye.
- `src/backend/app/infrastructure/celery/beat.py:27-52`: TÜİK kaynakları, TCMB kaynakları ve haber görevleri schedule’a ekleniyor.
- `src/backend/app/ingestion/data/tuik/schedule.py:10-29`: her kaynak için `schedule_seconds` metadata’sı yoksa günlük `86400.0`; minimum 60 saniye.
- `src/backend/app/ingestion/data/tcmb/schedule.py:21-39,42-90`: seri, katalog, takvim ve probe görevlerinin çoğu günlük; exchange-rates saatlik, announcements 6 saatlik.
- `src/backend/app/infrastructure/celery/app.py:24-45`: schedule Celery config’e veriliyor, timezone `Europe/Istanbul`, görev kuyruğu `production`.
- `docker-compose.yml:171-185`: beat `/tmp/celerybeat-schedule` ile başlıyor; beat için volume yok. Tanımlı named volume listesi `postgres_data`, `neo4j_data`, `neo4j_logs`, `redis_data`, `minio_data` (`:187-192`), schedule volume’u yok.


Celery interval schedule ilk açılışta görevleri “hemen çalıştır” olarak tanımlamıyor; interval entry’nin `last_run_at` değeri başlangıç zamanına yakın kabul edildiğinden ilk due zamanı interval sonrasıdır. Bu repo için günlük görev ilk beat sürecinden yaklaşık 24 saat sonra due olur; beat konteyneri bundan önce yeniden başlarsa görev hiç publish edilmemiş görünür. Bu davranış, repo kodundaki interval tanımlarının ve PersistentScheduler dosya yolunun sonucudur; canlı daemon log’u bu rapor kapsamında yeniden başlatılmadı.

`/tmp/celerybeat-schedule` PersistentScheduler state’idir, fakat container filesystem’indedir. Container yeniden yaratılırsa schedule state’i kaybolur; sayaç/`last_run_at` yeniden başlar ve günlük ilk tetik yine yaklaşık 24 saat sonraya ötelenir. Container yalnız restart edilip `/tmp` korunursa davranış farklı olabilir; Compose tanımında bu kalıcılık garanti edilmemiştir.

Bu, canlı gözlemde heartbeat görülüp production TÜİK/TCMB connector kaydı görülmemesiyle uyumludur: 30 saniyelik heartbeat sıkça due olur, günlük connector görevleri ise ilk interval’i doldurmadan çalışmamış olabilir. Worker’ın `production` kuyruğunu dinlediği de `docker-compose.yml:155-169` ile doğrulanır. Görev gerçekten publish edildikten sonra ayrıca runtime hata/commit sorunu olabilir; mevcut yerel kod yalnız başına bunu kanıtlamaz.

### Küçük ve güvenli öneri

Uygulama yapılmadan öneri:

1. Connector bootstrap/backfill görevleri için ayrı bir `on_startup` veya kısa gecikmeli bir one-shot Celery görevi ekleyip ilk keşif/çekimi açıkça tetiklemek; günlük schedule’ı korumak.
2. `/tmp/celerybeat-schedule` için named volume kullanmak veya host/container yaşam döngüsünden bağımsız kalıcı schedule path’i vermek.
3. İlk tetik garanti edilmek isteniyorsa günlük interval yerine uygun `crontab` yayın penceresi kullanmak; yine de ilk bootstrap’i crontab’a bırakmamak.

En küçük güvenli değişiklik, schedule dosyasını kalıcı yapmak ve connector bootstrap’i bir kez açıkça kuyruğa almaktır. Bu rapor yalnız öneridir; kod değiştirilmedi.

**Güven:** Schedule/config ve volume durumu **doğrulandı (A)**; canlı üretim kaydının tek kök nedeni **muhtemel**, çünkü worker/runtime hata logları bu incelemede yeniden alınmadı.

## 6. Task 2.2 ve 2.7 için en küçük dürüst yol

### Task 2.2 yapısal kabulü

En küçük dürüst yol, yeni UFE/NACE eşlemesi uydurmak değil, arşivde zaten kanıtı bulunan TÜFE ailesini kullanmaktır:

1. `DF_TUFE_SDMX_TT10` genel seri (`COICOP_2018=0`) ve `DF_TUFE_SDMX_TT02` gerçek bir alt grup (`COICOP_2018=01111`) veya `TT09` ana grup (`COICOP_2018=01`) çekilsin.
2. `CL_COICOP2018` içindeki gerçek parent-child zinciri ayrı sınıflama snapshot’ı olarak alınsın. Arşivde codelist kodları ve üst gruplar A seviyesinde var (`tufe-completion-tests.md:7-28`); fakat raporlanan örnek zincirin doğrudan `ust_kod` JSON’u ayrıca 1332 COICOP sınıflama kanıtından seçilmelidir.
3. Repo’daki `siniflama-coicop2018` kaynağı (`connector.py:570-574`) kullanılsın; ISIC kaynağı bu ilişkiye karıştırılmasın.
4. Ortak ay ve ortak `F_TFE` varyantı seçilerek iki gerçek seri/vintaj yazılsın; yapısal kanıtın dayandığı COICOP kodu evidence’a kaydedilsin.

Bu, Task 2.2’nin roadmap’teki “TÜFE ile TÜFE alt grubu” kabul kriteriyle doğrudan uyumludur (`roadmap/faz-2-deterministik-cekirdek.md:81-83`). ÜFE/ISIC adayları bu kabul için gerekli değildir ve kod uyumu doğrulanmadan kullanılmamalıdır.

### Task 2.7 istatistiksel kabulü

En az istekle gerçek ve dürüst aile:

- TÜİK `TT10` genel TÜFE.
- TÜİK `TT02` alt sınıfı veya `TT09` ana grubu.
- TÜİK `DF_YIUFE_EDO_V1` Yİ-ÜFE.
- BH ailesini iki-üç çifte çıkarmak için `DF_UFE_INSAAT_V1` ve/veya `DF_HUFE_EDO_V1`.

Arşiv XML’leri zaten 2005–2024 (inşaat için 2015–2024, HÜFE için 2017–2024) ortak aylıkları taşıyor. Bu nedenle yeni canlı veri keşfi yerine, **en az 4–5 lokal ham XML parse/normalize işlemi** yeterli olabilir; canlı connector kabulü isteniyorsa aynı dataflow’lar için her dataflow başına bir keşif/çekim isteği ve tarih aralığı kontrollü retry gerekir. İstatistiksel karar için:

- `DEGISIM=1` endeks seviyesini doğrudan korele etmeyin; fark/detrend seri kullanın.
- Ortak zaman kümesini hesaplayın, `n>=12` kontrol edin.
- 3–5 çifti tek `hypothesis_family` içinde tutun; BH’yi ham p değerleri üzerinde karar anında çalıştırın.
- Sonuç “nedensellik” değil, mekanik istatistiksel ilişki olarak adlandırılsın.

### Riskler ve bilinmeyenler

- TÜİK SDMX gerçek canlı endpoint’i token ve geçici timeout davranışı gösteriyor (`tuik/00-arastirma-plani.md:18-21`, `tufe-completion-tests.md:35-44`); canlı yeniden çekim arşiv XML’inden farklı sonuç verebilir.
- `COICOP_2018` kod listesindeki varlık, her dataflow/yayım kombinasyonunda seri garantisi değildir.
- UFE boyutlarının CPA/NACE adlandırması ile ISIC ağacının aynı kod sistemi olduğu kanıtlanmadı.
- TCMB’de aylık seri listesi geniş olsa da `TP.FG.J0` ve belirli faiz kodlarının 60+ kapsamı bu arşivde doğrulanmadı.
- Aday çiftler için korelasyon/p değeri hesaplanmadı; “istatistiksel olarak kabul edildi” denemez.
- `DF_YAPISAL_EFGTG_NACEREV21` gerçek NACE Rev.2.1 faaliyet verisi taşır, ancak yıllık olduğu için aylık 60+ yapısal aday değildir.

**Net öneri:** Task 2.2 için `TT10 ↔ TT02/TT09` ve COICOP-2018 sınıflama zinciri; Task 2.7 için aynı TÜİK arşivinden TT10, TT02/TT09, Yİ-ÜFE ve iki ek aylık UFE ailesi kullanılsın. Gerçek veriyle kanıtlanmamış UFE→ISIC eşlemesi ve TCMB seri kodu eklenmesin.

## Açık sorular

1. Canlı `databrowser2` üzerinde seçilen `TT02`/`TT09` seri anahtarlarının güncel yayım döneminde aynı 240 aylık kapsamı koruduğu tekrar doğrulandı mı?
2. COICOP-2018 `surumId=1332` gerçek JSON’unda `0 -> 01 -> ...` parent-child zincirinin tamamı hangi alanlarla dönüyor?
3. UFE’de `FAALIYET_CPA_2_1` kodları ile NACE Rev.2.1/ISIC Rev.4 arasında resmi ve birebir mapping var mı?
4. TCMB’de `TP.FG.J0` ve politika/faiz adaylarının katalogdan gerçek seri kodu ve bounds cevabı nedir?
5. Celery beat canlı container’ı ne kadar süre kesintisiz çalıştı ve worker’da connector task’ları için hata/ack/commit kaydı var mı?
6. Task 2.7 için seçilecek 3–5 çiftte dönüşüm, pencere ve BH ailesi sabitlenince gerçek p değerleri ve kabul kararı nedir?
