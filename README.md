# AWS Architecture & FinOps Review

Solutions Architect 포트폴리오를 위한 **읽기 전용 AWS 진단 대시보드**입니다. 여러 리전의 리소스, 비용 추세, AWS 추천, 운영 이상 신호와 S3 스토리지 비용을 한 화면에서 검토합니다.

![AWS Architecture and FinOps dashboard](output/playwright/dashboard-overview.png)

![S3 storage cost lab](output/playwright/s3-cost-lab.png)

## What it answers

- 어떤 AWS 리소스가 어느 리전에 존재하는가?
- 최근 6개월 비용은 어떻게 변했고 어떤 서비스가 지배적인가?
- Cost Optimization Hub가 제안하는 월 절감액과 실행 난이도는 무엇인가?
- 현재 ALARM 상태나 EC2 상태 이상이 있는가?
- S3 비용이 저장, 인출, 요청 중 어디에서 발생하는가?
- 미완료 멀티파트, noncurrent version, lifecycle 누락이 있는가?
- Standard / Standard-IA / Glacier Instant Retrieval / Intelligent-Tiering 중 무엇이 유리한가?

## Dashboard

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run dashboard.py
```

대시보드는 안전한 `Portfolio demo`로 시작합니다. `Live AWS`를 선택하고 **Refresh snapshot**을 눌러야만 AWS API를 호출합니다. 자격증명이나 실계정 결과는 저장소에 저장하지 않습니다.

### JSON collector

```bash
# AWS 호출 없는 데모 리포트
python aws_audit.py --demo

# 기본 읽기 전용 점검
python aws_audit.py --profile my-readonly --region ap-northeast-2 --region us-east-1

# 미완료 multipart의 모든 part 크기까지 계산
python aws_audit.py --profile my-readonly --s3-depth deep --max-api-cost 1.00
```

`--max-api-cost`는 Cost Explorer, CloudWatch, S3 LIST 호출의 보수적 상한입니다. 실제 청구서 계산기가 아니라, 깊은 스캔이 설정 금액을 넘기 전에 중단시키는 안전장치입니다.

## Coverage

| 목적 | 2026 AWS source | 구현 |
|---|---|---|
| 리소스 인벤토리 | AWS Resource Explorer | 전체 검색, EC2/RDS/Lambda fallback |
| 비용 추세 | AWS Cost Explorer | 월별·서비스별 6개월 비용 |
| 비용 이상 | AWS Cost Anomaly Detection | 최근 90일 anomaly |
| 절감 백로그 | AWS Cost Optimization Hub | 절감액, 노력, restart/rollback |
| 운영 문제 | CloudWatch + EC2 status | ALARM, instance/system check, scheduled event |
| S3 용량 | S3 daily CloudWatch metrics | bucket·storage class·object count |
| S3 구조 | S3 read-only APIs | versioning, lifecycle, logging, incomplete MPU |
| S3 비용 | Cost Explorer usage type | 저장·인출·요청 비용 분해 |
| S3 클래스 선택 | TCO simulator | 용량·객체 수·읽기 빈도·cold 비율 비교 |

Compute Optimizer는 Cost Optimization Hub가 수집하는 주요 추천 소스이므로 동일 추천을 별도 API로 중복 수집하지 않습니다. 조직 규모의 상세 원가 배부는 AWS Data Exports + QuickSight/CUDOS가 더 적합합니다.

## Safety

- 읽기 전용 IAM 정책: [`iam-policy.json`](iam-policy.json)
- 쓰기·삭제·lifecycle 변경 API 없음
- 객체 본문 `GetObject` 호출 없음; Glacier retrieval을 유발하지 않음
- 선택 서비스가 미등록/권한 부족이어도 나머지 섹션은 계속 수집
- 실계정 ID, ARN, bucket 이름은 데모 데이터에 포함하지 않음

## Validation

```bash
python -m unittest discover -s tests -v
python -m py_compile aws_audit.py dashboard.py
python aws_audit.py --demo --output /tmp/aws-tools-demo.json
```

설계 근거는 [`docs/architecture.md`](docs/architecture.md), S3 분석 방법은 [`docs/s3-cost-review.md`](docs/s3-cost-review.md), 검증 가능한 작업 이력은 [`docs/portfolio-timeline.md`](docs/portfolio-timeline.md)를 참고하세요.

기존 `ec2_analyzer.py`, `rds_analyzer.py`는 상세 CloudWatch 통계 분석용으로 유지됩니다. 새 대시보드는 계정 전체의 우선순위를 찾고, 기존 분석기는 선택한 EC2/RDS를 깊게 확인하는 2단계 구조입니다.
