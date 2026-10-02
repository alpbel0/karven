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
**Durum:** Başlamadı
**Bağımlılıklar:** —

**Referanslar:** `docs/catalog/concept-tree-draft.md`, `docs/DECISIONS.md` §7

**Hedef dosyalar:** `docs/catalog/concept-tree-draft.md`

### Checklist

- [ ] Eski 18 ana dallı ağacı kullanıcıyla birlikte gözden geçir (**proje içinde
      karar**, `docs/DECISIONS.md` → Proje içinde verilecek kararlar #2).
- [ ] Arama için gerekli etiket gruplarının (konu, ölçü, coğrafya, kırılım gibi)
      ağaçta yer aldığını doğrula.
- [ ] Onaylanan ağacı makinece okunabilir bir dosya olarak da sakla.

**Kabul kriteri:** Kullanıcı onaylı etiket ağacı dosyada; her etiketin kısa bir
tanımı var.

## Task 2.2 — Katalog tablosu

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 1.2, Task 1.3, Task 2.1

**Referanslar:** `docs/DECISIONS.md` §7

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] TÜİK ve TCMB seri listelerini tek katalogda topla (meta bilgisiyle).
- [ ] Her seri için etiketler ve okuyucu için tek cümlelik açıklama alanı (açıklama
      aramada kullanılmaz).
- [ ] Serinin yüklü mü yoksa talep üzerine çekilebilir mi olduğunu tut.

**Kabul kriteri:** İki kurumun seri listesi katalogda; her seri kurum, frekans
ve birim bilgisiyle sorgulanabiliyor.

## Task 2.3 — Jev ile etiketleme

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 2.2, Task 0.5

**Referanslar:** `docs/DECISIONS.md` §7

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Tek etiketleyici **Jev**; önce TypeSafe kredisi, bitince OpenRouter'daki Jev.
- [ ] Etiketleme yalnızca meta bilgiyle (ad, kurum kategorisi/tablosu, birim,
      frekans, kırılım); değer okunmaz.
- [ ] Güven eşiğinin üstü otomatik kabul; altındakiler Claude'un inceleme
      kuyruğuna düşer. Eşik **kalibrasyonla** belirlenir: ilk ~200 seri
      etiketlenip Claude tarafından kontrol edilir, hataların yığıldığı güven
      seviyesine göre eşik seçilip `docs/DECISIONS.md`'ye yazılır.
- [ ] Her seriye **tür** etiketi de verilir: fiyat / oran / endeks mi, akım
      (tutar, miktar) mı; birim (TL, döviz, %, adet, endeks) doğrulanır. Frekans
      eşleştirme ve enflasyondan arındırma bunu kullanır (`docs/DECISIONS.md` §8).
- [ ] Yeni eklenen seriler de etiketlenir (seri başına bir kez).

**Kabul kriteri:** Katalogdaki bütün seriler etiketli ya da inceleme kuyruğunda;
rastgele 50 seride etiketler elle kontrol edilmiş.

## Task 2.4 — Katalog arama aracı (Jev + kod + Jev)

**Repo:** `karven`
**Alan:** `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 2.3

**Referanslar:** `docs/DECISIONS.md` §7

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Ajan isteğini düz dille verir (ör. "Türkiye geneli toplam konut satış
      sayısı, aylık"); ajan etiketleri görmez.
- [ ] Jev etiket ağacındaki **her etiketi** istekle karşılaştırıp **1-5 puanlar**;
      4-5 alanlar geçer.
- [ ] Kod, geçen etiketlerden **herhangi birini** taşıyan serileri arar.
- [ ] Jev gelen serileri yeniden **1-5 puanlar**; liste büyükse parçalar halinde.
- [ ] **4-5 alan seriler** ajana döner (ad + meta bilgi, değer yok); hiçbiri 4-5
      almazsa "güçlü eşleşme yok" döner.
- [ ] Ajan başına arama sayısı sınırı: en fazla 3-4.

**Kabul kriteri:** "Türkiye geneli toplam konut satış sayısı, aylık" isteği doğru
seriyi 4-5 puanla döndürüyor; puanlama parçalı listede de çalışıyor.

**Not (2026-09-30):** Checklist'teki eski "seri" adımları DECISIONS §7'deki yeni
akışa göre (veri seti → hiyerarşik kırılım seçimi → düşük güvende çoklu
cümleleme → son doğrulama) yeniden yazılacak. Task 1.2c'de aynı işi yapan
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
