# Faz 2 — Katalog

## Faz amacı

Ajanların doğru seriyi bulabilmesi için etiketli bir katalog kurmak. Seriler
kapalı bir etiket listesinden (kavram ağacı) Jev ile etiketlenir. Arama
embedding'siz yapılır: Jev etiketleri puanlar, kod arar, Jev serileri puanlar,
4-5 alanlar ajana gider. Ajanlar etiketleri ve değerleri görmez; sadece seri
adları ve meta bilgisi görür. Arama kalitesi küçük bir test setiyle ölçülür.

## Task 2.1 — Kavram ağacının gözden geçirilmesi

**Repo:** `karven`
**Alan:** `docs`
**Durum:** Tamamlandı (kullanıcı onayladı, 2026-10-04); deneme etiketlemesi Task 2.3'te
**Bağımlılıklar:** —

**Referanslar:** `backend/app/catalog/concept_tree.yaml`, `docs/catalog/concept-tree-draft.md` (eski), `docs/DECISIONS.md` §7

**Hedef dosyalar:** `backend/app/catalog/concept_tree.yaml`, `backend/tests/test_catalog_concept_tree.py`

### Checklist

- [x] Eski 18 ana dallı ağacı kullanıcıyla birlikte gözden geçir (kullanıcı önerileri
      onayladı: 14. dal bölündü, 18. dal kalktı, tür ölçü boyutundan türetilir).
- [x] Arama için gerekli etiket gruplarının (konu, ölçüm türü, veri niteliği; coğrafya
      vb. kırılım/meta) ağaçta yer aldığını doğrula. Opus 5.5 + Codex gpt-6-astra 2 tur inceledi.
- [x] Onaylanan ağacı makinece okunabilir bir dosya olarak da sakla
      (`concept_tree.yaml` + bütünlük testi).
- [x] Gerçek TÜİK/TCMB/HMB veri setleri v2.1 etiketlerine karşı deneme etiketlemesinde
      (Task 2.3 kalibrasyonu) çakışma/boşluk için tekrar gözden geçirildi: tek boşluk
      `finansal_hesaplar` ailesiydi (dal tanımı düzeltildi); insani gelişme endeksi için yaprak yok.

**Kabul kriteri:** Kullanıcı onaylı etiket ağacı dosyada; her etiketin kısa bir
tanımı var.

## Task 2.2 — Katalog tablosu

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Tamamlandı (canlıda doğrulandı, 2026-10-04)
**Bağımlılıklar:** Task 1.2, Task 1.3, Task 2.1

**Referanslar:** `docs/DECISIONS.md` §7 (Katalog zenginleştirme), §8, `backend/app/catalog/concept_tree.yaml`

**Hedef dosyalar:** `backend/app/catalog/enrich*.py`, `tree.py`, `status.py`, `prompts/`,
migration `0018_catalog_enrichment.py`

### Checklist

- [x] TÜİK ve TCMB seri listelerini tek katalogda topla (meta bilgisiyle). Faz 1'de
      kuruldu: 883 veri seti (TÜİK 616, TCMB 243, HMB 24) + boyut kod listeleri.
- [x] Etiket alanı (`dataset_tags`, Task 2.3 doldurur) ve okuyucu açıklaması (veri seti
      düzeyinde şablon; 883/883 dolu; kaynağın açıklaması `source_description`'da).
- [x] Yüklü mü / talep üzerine mi: saklanmaz, gözlemlerden hesaplanır (`catalog/status.py`).
- [x] Veri seti meta bayrakları dolu ve testli (883/883 `flags_checked_at`): arsiv 63,
      revizyon_tablosu 16, cok_konulu_derleme 7, donem_serisi 25; para birimi / nominal /
      mevsim kümeleri ölçü birleşimlerinden.
- [x] Ölçü birleşimleri türe, veri niteliğine, toplama kuralına, para birimine ve
      nominal/reel'e eşlendi: 30.994 birleşim, hepsi kabul (kural 18.773, Jev 11.877,
      elle 344); TCMB toplama yöntemi `source_aggregation`'da. Rastgele 50 örnekte tür 50/50
      doğru (kontrol sırasında bulunan nominal hatası düzeltildi).

**Kabul kriteri:** İki kurumun seri listesi katalogda; her seri kurum, frekans
ve birim bilgisiyle sorgulanabiliyor; meta bayrakları dolu ve testle denetleniyor.

## Task 2.3 — Jev ile etiketleme

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Tamamlandı (canlıda doğrulandı, 2026-10-04)
**Bağımlılıklar:** Task 2.2, Task 0.5

**Referanslar:** `docs/DECISIONS.md` §7, `backend/app/catalog/concept_tree.yaml`

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [x] Tek etiketleyici **Jev**; önce TypeSafe kredisi, bitince OpenRouter'daki Jev.
- [x] Etiketleme yalnızca meta bilgiyle (ad, kurum kategorisi/tablosu, birim,
      frekans, kırılım); değer okunmaz.
- [x] Güven eşiğinin üstü otomatik kabul; altındakiler Claude'un inceleme
      kuyruğuna düşer. Eşik **kalibrasyonla** belirlenir: ilk ~200 seri
      etiketlenip Claude tarafından kontrol edilir, hataların yığıldığı güven
      seviyesine göre eşik seçilip `docs/DECISIONS.md`'ye yazılır.
- [x] Etiketleme **veri seti düzeyinde**, tam `category_path` ile yapılır; iki aşama (ana dal
      ≥0,40 geçer, yaprak ≥0,60 aday); 1-3 yaprak, 3'ten fazlaysa en yüksek 3'ü alınır, kesilenler
      `rejected` kaydedilir (çok konulu derleme istisnası: yalnız ana dal). Hiç yaprak ≥0,60
      değilse veri seti inceleme kuyruğuna gider (`etiketsiz` yok). Kararlar: DECISIONS §7.
- [x] **Tür** Jev'den gelmez: ölçü kodu → ölçüm türü eşlemesi Task 2.2'de bir kez
      yapılır (fiyat / oran / endeks, akım, stok, yüzde değişim ...; birim doğrulanır).
      Frekans eşleştirme ve enflasyondan arındırma bunu kullanır (`docs/DECISIONS.md` §8).
      Jev etiketi kategori ipucu tablosuyla uyuşmazsa veri seti inceleme kuyruğuna gider.
- [x] Yeni eklenen veri setleri de etiketlenir (veri seti başına bir kez).

**Kabul kriteri:** Katalogdaki bütün veri setleri etiketli ya da inceleme kuyruğunda;
rastgele 50 veri setinde etiketler elle kontrol edilmiş.

## Task 2.4 — Katalog arama aracı (Jev + kod + Jev)

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Tamamlandı (canlıda doğrulandı, 2026-10-05)
**Bağımlılıklar:** Task 2.3

**Referanslar:** `docs/DECISIONS.md` §7 (arama akışı), `backend/app/catalog/concept_tree.yaml`

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [x] Ajan isteğini düz dille verir (ör. "Türkiye geneli toplam konut satış
      sayısı, aylık"); ajan etiketleri görmez.
- [x] **Veri seti seçimi:** Jev önce 24 ana dalı, sonra yalnız geçen ana dalların
      yapraklarını **1-5 puanlar**; 4-5 alanlar geçer.
- [x] Kod, geçen yapraklardan **herhangi birini** taşıyan veri setlerini ve geçen ana
      dalı taşıyan çok konulu derlemeleri (Turcat, PKA) getirir; `revizyon_tablosu`
      olanlar dışlanır. Jev aday veri setlerini yeniden **1-5 puanlar** (büyük listede
      parçalar halinde).
- [x] **Kırılım seçimi:** kod boyutları sırayla Jev'e sorulur, **hiyerarşik** (önce
      düzey sonra yer; önce ana sektör sonra alt sektör); seçim sorusu en fazla 255
      seçenek alır.
- [x] Güven eşiğin altındaysa aynı soru 2-3 farklı cümleyle sorulur; cevaplar
      uyuşursa kabul, uyuşmazsa en iyi adaylar tutulur.
- [x] **Son doğrulama:** seçilen serinin tam adıyla "bu seri isteğe uyuyor mu?";
      düşükse sıradaki aday seriye / veri setine geçilir.
- [x] **4-5 alan seriler** ajana döner (ad + meta bilgi, ölçüm türü, frekans, birim;
      değer yok). Hâlâ belirsizse en iyi 2-3 aday sunulur; hiçbiri 4-5 almazsa
      "güçlü eşleşme yok" döner.
- [x] Ajan başına arama sayısı sınırı: en fazla 3-4.

**Kabul kriteri:** "Türkiye geneli toplam konut satış sayısı, aylık" isteği doğru
seriyi 4-5 puanla döndürüyor; puanlama parçalı listede de çalışıyor.

**Sonuç (2026-10-05, canlı):** Kabul sorgusu `DF_SATIS_SEKLI_SATIS_DURUMU_V3:M._T.TR.3._T.MII_KSS`
serisini p(4)+p(5)=0,71 ile döndürdü. Kararlar, canlı bulgular ve ölçümler: `docs/DECISIONS.md` §7
("Arama sonucu"). Araç adı `find_series` (`backend/app/catalog/search.py`, CLI
`python -m app.catalog.search`); fetch ajanının alt dize aramalı `search_catalog` aracı ayrıdır.
Arama sınırı mekanizması (`tool_loop` `tool_limits`) gerçek EVREN döngüsüyle canlı doğrulandı;
ajanlara bağlama Task 3.x / 5.x'te yapılır. 131 veri setinin frekans boşluğu Task 2.4b'de çözüldü.

**Not (2026-09-30, checklist 2026-10-04'te yeni akışa göre yeniden yazıldı):** DECISIONS §7'deki
akış (veri seti → hiyerarşik kırılım seçimi → düşük güvende çoklu
cümleleme → son doğrulama) checklist'e işlendi. Aşağıdaki gözlemler korunuyor:
Task 1.2c'de aynı işi yapan
eşleştirme prototipinden (`app/catalog/linking.py`) canlıda öğrenilenler:
Türkçe istek ile TÜİK'in **İngilizce** etiketleri arasında uyumsuzluk (ör.
"Nüfus"u doğru tabloda bulup doğrulamada 0,28 ile eledi); ilk adımda yanlış
veri seti seçimi (GSYH sektör bileşenleri yıllık/bölgesel tablolara gitti);
veri seti seçimine açıklama ve kategori bilgisinin katılması gerekiyor. Turcat
ve CİP eşleştirmeleri bu yüzden elle yapıldı (kullanıcı kararı).

## Task 2.4b — Frekansı kurulamayan veri setleri

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Tamamlandı (canlıda doğrulandı, 2026-10-05)
**Bağımlılıklar:** Task 2.4

**Referanslar:** `docs/DECISIONS.md` §7 ("Arama kararları" ve "Frekans boşluğu"),
`backend/app/connectors/tuik/frequency.py`, `backend/app/connectors/base.py`
(`build_series_definition`, `_merge_frequency_attributes`)

**Neden:** Canlı aramada `DF_MEVSIM_TAKVIM_V3` (aylık, 2013-2026) için seri kurulamadı: veri setinde
`FREQ` boyutu, kod düzeyinde frekans özniteliği ve `default_frequency` yok. Sayım (2026-10-05):
883 veri setinin **131'i (%15)** bu durumda: TÜİK databrowser2 **90** (frekans kaynakta gizli `FREQ`
boyutunda) ve veriportali **41** (boyutsuz, indirilemeyen rapor girdileri). Hem arama hem veri çekme
(`ensure_series`) bunlardan seri kuramazdı.

### Checklist

- [x] 131 veri setinin kaynakta nasıl göründüğü ölçüldü: databrowser2'nin 90'ında kaynağın gizli
      `FREQ` kod listesi frekansı doğrudan veriyor (tahmin ve dönem biçimi gerekmedi); veriportali'nin
      41'inde boyut, kapsam ve gözlem yok, hepsi `downloadable: false`.
- [x] Kural kullanıcıya gösterildi ve onaylandı: dry-run 90/90 `single` (88 yıllık, 2 aylık), 28'i
      `A2` "Biennial". Kullanıcı kararı: kelime dağarcığına yeni **`biennial`** frekansı eklenir.
- [x] Kural bağlayıcıya ve doldurma komutuna **aynı fonksiyonla** işlendi (`resolve_frequency`):
      tek geçerli kod `default_frequency` olur, birden çok kod / boş liste / sorgu hatası / tanınmayan
      kod ayrı nedenle raporlanır, başarısız sorgu mevcut değeri silmez.
- [x] Canlı katalog güncellendi (`frequency-backfill`: 90 veri seti, `default_frequency` 102 → 192) ve
      katalog yenilemesi doğrulandı: değerler aynı kaldı, `checked_at` kaynaktan yenilendi, `tagging`
      işareti 883/883 sağlam. Gözlem değeri çekilmedi (yalnız kod listeleri).
- [x] `veri_yok` bayrağı (migration 0019): indirilemeyen 41 veriportali girdisi işaretlendi ve aramadan
      dışlandı. Seri kurulamayan işaretsiz veri seti kalmadı; `DF_MEVSIM_TAKVIM_V3` aramada serisiyle
      çıkıyor (aylık, p=0,78), atık ailesi `biennial` olarak bulunuyor.

**Kabul kriteri:** Katalogdaki bütün veri setleri için seri tarifi kurulabiliyor (41 `veri_yok` hariç);
"Mevsim ve takvim etkisinden arındırılmış konut satış sayısı, aylık" isteği `DF_MEVSIM_TAKVIM_V3`
serisini döndürüyor. Sağlandı.

**Ek düzeltme (aynı iş):** Katalog yenilemesi `attributes['tagging']` işaretini siliyordu (Task 2.3'ten
kalan hata; sonraki `tag run` veri setlerini yeniden etiketleme adayı sayardı). Koruma listesine eklendi,
testle kanıtlandı.

## Task 2.5 — Veri durumu aracı

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Tamamlandı (2026-10-05)
**Bağımlılıklar:** Task 2.2

**Referanslar:** `docs/DECISIONS.md` §4

**Hedef dosyalar:** `backend/app/catalog/status.py` (araç `data_status`, CLI
`python -m app.catalog.status <kurum> <veri_seti> '<kodlar JSON>'`), testler
`tests/test_catalog_status_tool.py` ve `tests/integration/test_catalog_status_db.py`.

### Checklist

- [x] Bir seri için: var mı, yüklü mü, kapsadığı yıllar, frekans, birim, uygun
      dönüşüm (seviye / yüzde değişim).
- [x] **Değer döndürmez.**

**Kullanıcı kararları (2026-10-05):** (1) Girdi `find_series` reçetesi (kurum + veri seti + kodlar);
seri satırı henüz yoksa da cevap verir. (2) Kapsam iki ayrı alanda: `available_range` (kaynağın
sunduğu, veri setinden) ve `loaded_range` (bizde yüklü, gözlemlerden; yüklü değilse `null`).
(3) Dönüşümler katalogdaki ölçü bilgisinden (`measure_combinations`) okunur, birimden tahmin
edilmez; ölçü bilinmiyorsa `transforms: null`. (4) Çağrı başına en fazla 3 reçete, fazlası
`skipped` notuyla atlanır.

**Dönüşüm kuralı:** `level` ölçü bilindiğinde her zaman; `percent_change_period` yalnız miktar
ölçülerinde (tutar, adet, fiziksel, fiyat/kur, endeks, kişi başına), kümülatif değilse ve frekans
günlük...yıllık ise; `percent_change_annual` ayrıca yalnız aylık, çeyreklik, altı aylık. Zaten %
değişim, oran, pay, ağırlık, dağılım olan ölçüler yalnız `level`. `biennial`/`irregular` yalnız `level`.

**Not:** `Series.coverage_*` yüklenen gözlemlerle genişlediği için kaynak aralığı için kullanılmaz.
Bazı kaynaklarda veri seti `coverage_end` ay sonu (ör. 2026-08-31), `loaded_range` ise dönem başı
(2026-08-01) olarak görünür.

**Kabul kriteri:** Ajan bir serinin kapsamını ve frekansını öğrenebiliyor; araç
çıktısında hiçbir gözlem değeri yok. Sağlandı: birim 1212, entegrasyon 119 test geçti; canlıda
yüklenmemiş (`DF_MEVSIM_TAKVIM_V3`) ve yüklü (260 dönem) seri doğru döndü, çıktıda `value` yok.

## Task 2.6 — Arama test seti ve ölçüm

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 2.4

**Referanslar:** `docs/DECISIONS.md` §7

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] **30-40 sorguluk** test seti: istek → beklenen seri(ler).
- [ ] Ölçüm: beklenen seri 4-5 alanlar arasında mı.
- [ ] Sonuç yetersizse embedding sıralaması (OpenRouter
      `openai/text-embedding-3-large`) ayrı bir karar olarak değerlendirilir.

**Kabul kriteri:** Test seti kaydedildi, ölçüm tekrar çalıştırılabilir ve sonuç
raporlandı. Başarı eşiği kullanıcıyla belirlenir (**proje içinde karar** #4).
