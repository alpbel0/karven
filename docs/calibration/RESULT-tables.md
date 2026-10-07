## Yapısal olarak karar verilemeyen hücreler

Her tekrarın `veri yetersiz` olduğu (en az gözlem kuralı) yapı/boyut çiftleri: `k3_real_null_null` n=100, `k3_real_real_null` n=100, `k3_real_real_real` n=100. Bunlar yöntem zayıflığı değil, motorun `eşik × sürücü sayısı` kuralının sonucudur (üç sürücülü regresyon için aylıkta 108 gözlem).

## Bölge ailelerinde yanlış destekleme (nihai iki-pencere kararı, yalnız güvenilir tekrarlar)

Hücre = (aile, yapı, n). `toplu` = hata toplamı / güvenilir toplamı; `en kötü` = en yüksek tek hücre oranı (en az 200 güvenilir tekrarı olan hücreler). Karar kuralı iki taraflı p < 0,05 + doğru işaret; tek bir sabit testin ideali ~%2,5.

| aile | n | güvenilir pay | toplu yanlış destekleme | en kötü hücre | hücre sayısı |
|---|---|---|---|---|---|
| ar05 | 100 | 77.3% | 0.9% | 2.4% | 9 |
| ar05 | 250 | 99.5% | 0.2% | 0.8% | 9 |
| iid | 100 | 77.8% | 1.3% | 2.9% | 9 |
| iid | 250 | 100.0% | 0.2% | 1.0% | 9 |
| ma11 | 100 | 48.8% | 0.7% | 2.7% | 9 |
| ma11 | 250 | 59.5% | 0.1% | 0.6% | 9 |
| sar12_05 | 100 | 24.7% | 2.6% | 4.6% | 9 |
| sar12_05 | 250 | 6.5% | 0.0% | yetersiz | 9 |
| seasonal_weak | 100 | 76.8% | 2.0% | 4.9% | 9 |
| seasonal_weak | 250 | 99.0% | 0.7% | 1.9% | 9 |

## Kapının reddetmesi gereken aileler

`sızıntı` = (güvenilir ve yanlış destekleyen) / tüm tekrarlar; `güvenilir pay` = güvenilir / tüm tekrarlar. Değerler hücreler üzerinden en büyük olandır.

| aile | n | en büyük sızıntı | en büyük güvenilir pay |
|---|---|---|---|
| sar12_08 | 100 | 0.0% | 0.0% |
| sar12_08 | 250 | 0.0% | 0.0% |
| seasonal_common | 100 | 2.2% | 6.4% |
| seasonal_common | 250 | 0.3% | 0.5% |
| seasonal_indep | 100 | 0.4% | 3.6% |
| seasonal_indep | 250 | 0.0% | 0.5% |

## Güvenilir işaretleme payı (aile x sürücü sayısı x n)

| aile | n | k=1 | k=2 | k=3 |
|---|---|---|---|---|
| ar05 | 100 | 99.7% | 99.3% | 0.0% |
| ar05 | 250 | 99.9% | 99.5% | 99.6% |
| iid | 100 | 100.0% | 100.0% | 0.0% |
| iid | 250 | 100.0% | 100.0% | 100.0% |
| ma11 | 100 | 75.6% | 57.8% | 0.0% |
| ma11 | 250 | 76.0% | 59.1% | 41.0% |
| sar12_05 | 100 | 41.4% | 28.2% | 0.0% |
| sar12_05 | 250 | 13.0% | 6.0% | 1.7% |
| seasonal_weak | 100 | 99.2% | 98.4% | 0.0% |
| seasonal_weak | 250 | 99.3% | 98.8% | 98.4% |

## Güç (gerçek ilişki; yalnız güvenilir tekrarlar arasında desteklenen oran)

| yapı | n | güç |
|---|---|---|
| k1_real02 | 100 | 21.4% |
| k1_real02 | 250 | 21.5% |
| k1_real04 | 100 | 69.0% |
| k1_real04 | 250 | 73.0% |
| k1_real07 | 100 | 92.0% |
| k1_real07 | 250 | 91.7% |
| k2_real_nearzero | 100 | 6.4% |
| k2_real_nearzero | 250 | 4.6% |
| k2_real_real04 | 100 | 55.3% |
| k2_real_real04 | 250 | 61.1% |
| k2_real_real07 | 100 | 88.3% |
| k2_real_real07 | 250 | 88.8% |
| k2_real_spread | 100 | 29.6% |
| k2_real_spread | 250 | 34.8% |
| k3_real_real_real | 100 | yok |
| k3_real_real_real | 250 | 89.4% |

## Protokolün ön-kayıtlı %4 payını aşan ya da yeterli güvenilir tekrarı olmayan hücreler

| hücre | güvenilir | hata | oran |
|---|---|---|---|
| `stat/seasonal_weak/k1_null/n100` | 797 | 39 | 4.9% |
| `stat/sar12_05/k1_null/n100` | 324 | 15 | 4.6% |
| `stat/sar12_05/k2_real_null_r8/n100` | 252 | 11 | 4.4% |
| `stat/iid/k3_real_null_null/n100` | 0 | 0 | 0.0% |
| `stat/ar05/k3_real_null_null/n100` | 0 | 0 | 0.0% |
| `stat/ar05/k3_real_real_null/n100` | 0 | 0 | 0.0% |
| `stat/ma11/k3_real_real_null/n100` | 0 | 0 | 0.0% |
| `stat/ma11/k3_real_null_null/n100` | 0 | 0 | 0.0% |
| `stat/iid/k3_real_real_null/n100` | 0 | 0 | 0.0% |
| `stat/sar12_05/k1_reversed/n250` | 79 | 0 | 0.0% |
| `stat/sar12_05/k2_null_null/n250` | 27 | 0 | 0.0% |
| `stat/sar12_05/k2_real_null/n250` | 36 | 0 | 0.0% |
| `stat/sar12_05/k2_real_null_r5/n250` | 35 | 0 | 0.0% |
| `stat/sar12_05/k1_null/n250` | 62 | 0 | 0.0% |
| `stat/sar12_05/k2_real_null_r8/n250` | 61 | 0 | 0.0% |
| `stat/sar12_05/k2_real_reversed/n250` | 29 | 0 | 0.0% |
| `stat/sar12_05/k3_real_null_null/n250` | 12 | 0 | 0.0% |
| `stat/sar12_05/k3_real_null_null/n100` | 0 | 0 | 0.0% |
| `stat/sar12_05/k3_real_real_null/n100` | 0 | 0 | 0.0% |
| `stat/sar12_05/k3_real_real_null/n250` | 9 | 0 | 0.0% |
| `stat/seasonal_weak/k3_real_null_null/n100` | 0 | 0 | 0.0% |
| `stat/seasonal_weak/k3_real_real_null/n100` | 0 | 0 | 0.0% |
