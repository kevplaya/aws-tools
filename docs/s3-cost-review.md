# S3 cost review playbook

This playbook turns the June 2026 read-only S3 investigation into a repeatable, sanitized portfolio workflow.

## 1. Separate the bill before recommending a class

Use Cost Explorer grouped by `USAGE_TYPE` and distinguish:

1. storage byte-hours by class;
2. retrieval bytes;
3. GET/LIST requests;
4. data transfer;
5. early-delete or restore charges.

A low storage price alone is not a recommendation. Glacier Instant Retrieval can lose to Standard when a data set is repeatedly scanned because every retrieved GB and request is charged.

## 2. Inventory storage posture

For each bucket collect:

- region and creation time;
- daily `BucketSizeBytes` by storage class;
- daily `NumberOfObjects`;
- versioning status;
- lifecycle transitions;
- incomplete multipart abort rule;
- noncurrent-version expiration;
- server access logging;
- incomplete multipart upload count and, in deep mode, uploaded part bytes.

S3 Storage Lens is the native organization-scale alternative and adds historical and prefix-level metrics. The local scanner is useful when the account has not enabled advanced Storage Lens or when a portable portfolio demo is needed.

## 3. TCO model

For a data set of `G` GB, `N` objects and `R` full reads per month:

```text
monthly = G × storage_rate
        + R × G × retrieval_rate
        + R × (N / 1000) × GET_rate
        + monitoring_fee
```

The simulator includes the 2026-06 `ap-northeast-2` assumptions used during the original review. They are intentionally editable because AWS prices and account discounts change.

The original sanitized case was 26 TB, about 1.46 million objects, average object size about 18 MB:

- GIR was economical only below roughly 0.65 full reads/month;
- at one or more monthly reads, retrieval materially erased storage savings;
- Intelligent-Tiering monitoring was small because the files were large and object count per TB was low;
- a mixed hot/cold archive favored Intelligent-Tiering, while uniformly hot data favored Standard.

## 4. High-signal findings

### Incomplete multipart uploads

Uploaded parts are billed until completion or abort. Basic mode counts uploads. Deep mode calls `ListParts` for exact stranded bytes but never downloads objects.

The safe recommendation is a lifecycle rule such as abort after seven days. The dashboard reports the gap but deliberately does not apply it.

### Noncurrent versions

Versioning without `NoncurrentVersionExpiration` can hide large retained versions from current-object listings. Measure with Storage Lens or S3 Inventory before choosing retention.

### Access evidence

When server access logging and CloudTrail data events were not enabled, historical GET activity cannot be reconstructed. Athena/Glue history can be a useful proxy, not proof that no other client reads the objects.

## 5. Decision rules

| Access pattern | First candidate | Verify before action |
|---|---|---|
| Continuously hot | Standard | P95 access interval and request volume |
| Mixed/unknown, large objects | Intelligent-Tiering | Cold fraction and monitoring fee |
| Rare, immediate retrieval required | Glacier Instant Retrieval | Retrieval break-even and minimum duration |
| Truly cold, hours acceptable | Deep Archive | Restore RTO and minimum duration |
| Disposable generated data | Expiration | Owner, retention policy, legal/audit needs |

No class transition or deletion should be automated from a single snapshot.
