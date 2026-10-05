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

**Referanslar:** `docs/catalog/concept-tree.yaml`, `docs/catalog/concept-tree-draft.md` (eski), `docs/DECISIONS.md` §7

**Hedef dosyalar:** `docs/catalog/concept-tree.yaml`, `backend/tests/test_catalog_concept_tree.py`

### Checklist

- [x] Eski 18 ana dallı ağacı kullanıcıyla birlikte gözden geçir (kullanıcı önerileri
      onayladı: 14. dal bölündü, 18. dal kalktı, tür ölçü boyutundan türetilir).
- [x] Arama için gerekli etiket gruplarının (konu, ölçüm türü, veri niteliği; coğrafya
      vb. kırılım/meta) ağaçta yer aldığını doğrula. Opus 5.5 + Codex gpt-6-astra 2 tur inceledi.
- [x] Onaylanan ağacı makinece okunabilir bir dosya olarak da sakla
      (`concept-tree.yaml` + bütünlük testi).
- [ ] Gerçek TÜİK/TCMB/HMB veri setleri v2.1 etiketlerine karşı deneme etiketlemesinde
      (Task 2.3 kalibrasyonu) çakışma/boşluk için tekrar gözden geçirilir.

**Kabul kriteri:** Kullanıcı onaylı etiket ağacı dosyada; her etiketin kısa bir
tanımı var.

## Task 2.2 — Katalog tablosu

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 1.2, Task 1.3, Task 2.1

**Referanslar:** `docs/DECISIONS.md` §7, `docs/catalog/concept-tree.yaml`

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] TÜİK ve TCMB seri listelerini tek katalogda topla (meta bilgisiyle).
- [ ] Her seri için etiketler ve okuyucu için tek cümlelik açıklama alanı (açıklama
      aramada kullanılmaz).
- [ ] Serinin yüklü mü yoksa talep üzerine çekilebilir mi olduğunu tut.
- [ ] Veri seti meta bayraklarını kaynak bilgisinden doldur: `revizyon_tablosu`,
      `mevsim_arindirilmis`, `para_birimi`, `nominal_mi`, `donem_serisi`, `arsiv`,
      `cok_konulu_derleme` (2026-10-04'te DB'de hiçbirinde dolu değildi).
- [ ] Ölçü boyutu kodlarını bir kez ölçüm türüne (`measure_types`) ve gerekirse
      veri niteliğine eşle; kaynağın kendi toplama yöntemi varsa onu sakla.

**Kabul kriteri:** İki kurumun seri listesi katalogda; her seri kurum, frekans
ve birim bilgisiyle sorgulanabiliyor; meta bayrakları dolu ve testle denetleniyor.

## Task 2.3 — Jev ile etiketleme

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 2.2, Task 0.5

**Referanslar:** `docs/DECISIONS.md` §7, `docs/catalog/concept-tree.yaml`

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Tek etiketleyici **Jev**; önce TypeSafe kredisi, bitince OpenRouter'daki Jev.
- [ ] Etiketleme yalnızca meta bilgiyle (ad, kurum kategorisi/tablosu, birim,
      frekans, kırılım); değer okunmaz.
- [ ] Güven eşiğinin üstü otomatik kabul; altındakiler Claude'un inceleme
      kuyruğuna düşer. Eşik **kalibrasyonla** belirlenir: ilk ~200 seri
      etiketlenip Claude tarafından kontrol edilir, hataların yığıldığı güven
      seviyesine göre eşik seçilip `docs/DECISIONS.md`'ye yazılır.
- [ ] Etiketleme **veri seti düzeyinde**, tam `category_path` ile yapılır; 1-3 yaprak,
      aşan kapsam inceleme kuyruğuna gider (çok konulu derleme istisnası: yalnız ana dal).
- [ ] **Tür** Jev'den gelmez: ölçü kodu → ölçüm türü eşlemesi Task 2.2'de bir kez
      yapılır (fiyat / oran / endeks, akım, stok, yüzde değişim ...; birim doğrulanır).
      Frekans eşleştirme ve enflasyondan arındırma bunu kullanır (`docs/DECISIONS.md` §8).
      Jev etiketi kategori ipucu tablosuyla uyuşmazsa veri seti inceleme kuyruğuna gider.
- [ ] Yeni eklenen veri setleri de etiketlenir (veri seti başına bir kez).

**Kabul kriteri:** Katalogdaki bütün veri setleri etiketli ya da inceleme kuyruğunda;
rastgele 50 veri setinde etiketler elle kontrol edilmiş.

## Task 2.4 — Katalog arama aracı (Jev + kod + Jev)

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 2.3

**Referanslar:** `docs/DECISIONS.md` §7 (arama akışı), `docs/catalog/concept-tree.yaml`

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Ajan isteğini düz dille verir (ör. "Türkiye geneli toplam konut satış
      sayısı, aylık"); ajan etiketleri görmez.
- [ ] **Veri seti seçimi:** Jev önce 24 ana dalı, sonra yalnız geçen ana dalların
      yapraklarını **1-5 puanlar**; 4-5 alanlar geçer.
- [ ] Kod, geçen yapraklardan **herhangi birini** taşıyan veri setlerini ve geçen ana
      dalı taşıyan çok konulu derlemeleri (Turcat, PKA) getirir; `revizyon_tablosu`
      olanlar dışlanır. Jev aday veri setlerini yeniden **1-5 puanlar** (büyük listede
      parçalar halinde).
- [ ] **Kırılım seçimi:** kod boyutları sırayla Jev'e sorulur, **hiyerarşik** (önce
      düzey sonra yer; önce ana sektör sonra alt sektör); seçim sorusu en fazla 255
      seçenek alır.
- [ ] Güven eşiğin altındaysa aynı soru 2-3 farklı cümleyle sorulur; cevaplar
      uyuşursa kabul, uyuşmazsa en iyi adaylar tutulur.
- [ ] **Son doğrulama:** seçilen serinin tam adıyla "bu seri isteğe uyuyor mu?";
      düşükse sıradaki aday seriye / veri setine geçilir.
- [ ] **4-5 alan seriler** ajana döner (ad + meta bilgi, ölçüm türü, frekans, birim;
      değer yok). Hâlâ belirsizse en iyi 2-3 aday sunulur; hiçbiri 4-5 almazsa
      "güçlü eşleşme yok" döner.
- [ ] Ajan başına arama sayısı sınırı: en fazla 3-4.

**Kabul kriteri:** "Türkiye geneli toplam konut satış sayısı, aylık" isteği doğru
seriyi 4-5 puanla döndürüyor; puanlama parçalı listede de çalışıyor.

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

## Task 2.5 — Veri durumu aracı

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 2.2

**Referanslar:** `docs/DECISIONS.md` §4

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Bir seri için: var mı, yüklü mü, kapsadığı yıllar, frekans, birim, uygun
      dönüşüm (seviye / yüzde değişim).
- [ ] **Değer döndürmez.**

**Kabul kriteri:** Ajan bir serinin kapsamını ve frekansını öğrenebiliyor; araç
çıktısında hiçbir gözlem değeri yok.

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
