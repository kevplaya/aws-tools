# AWS Resource Analyzer

AWS CloudWatch 메트릭을 기반으로 EC2 및 RDS/Aurora 인스턴스를 분석하여 비용 최적화 추천을 제공하는 Python 도구입니다.

## Features

✨ **EC2 인스턴스 분석**
- CPU, 네트워크, 디스크 I/O 사용량 분석
- 과다/부족 프로비저닝 감지
- 인스턴스 타입 업/다운사이징 추천

💾 **RDS/Aurora 데이터베이스 분석**
- CPU, 메모리, IOPS, 지연시간 분석
- 스토리지 타입 최적화 (gp2→gp3 전환으로 20% 절감)
- Read Replica 필요성 분석
- Aurora 클러스터 분석 및 Replica Lag 모니터링

📊 **리포팅**
- 컬러풀한 콘솔 출력
- JSON/CSV 형식 리포트 생성
- 통계 분석 (평균, 최댓값, P95, P99)

## Quick Start

### 1. 설치

```bash
pip install -r requirements.txt
```

### 2. AWS 인증 설정

```bash
aws configure
```

필요한 정보:
- AWS Access Key ID
- AWS Secret Access Key
- Default region (예: ap-northeast-2)

### 3. 실행

**EC2 분석:**
```bash
python ec2_analyzer.py analyze
```

**RDS/Aurora 분석:**
```bash
python rds_analyzer.py analyze
```

## Usage Examples

### EC2 분석

```bash
# 모든 EC2 인스턴스 분석 (대화형)
python ec2_analyzer.py analyze

# 특정 인스턴스 분석
python ec2_analyzer.py analyze --instance-id i-1234567890abcdef0

# 30일 데이터로 분석 후 리포트 저장
python ec2_analyzer.py analyze --days 30 --output-json ec2_report.json --output-csv ec2_report.csv

# 특정 태그의 인스턴스만 분석
python ec2_analyzer.py analyze --tag Environment=Production

# 인스턴스 목록만 확인
python ec2_analyzer.py list-instances-cmd
```

### RDS/Aurora 분석

```bash
# 모든 RDS/Aurora 인스턴스 분석 (대화형)
python rds_analyzer.py analyze

# 특정 DB 인스턴스 분석
python rds_analyzer.py analyze --db-identifier mydb-instance

# Aurora 클러스터 전체 분석 (Writer + Readers)
python rds_analyzer.py analyze --cluster my-aurora-cluster

# 14일 데이터로 분석 후 리포트 저장
python rds_analyzer.py analyze --days 14 --output-json rds_report.json

# 클러스터 목록 확인
python rds_analyzer.py list-clusters
```

## Configuration

### EC2 설정 (`config.json`)

```json
{
  "thresholds": {
    "cpu_low": 20,          // CPU 낮음 임계값 (%)
    "cpu_high": 80,         // CPU 높음 임계값 (%)
    "network_high_mbps": 1000,
    "memory_high": 85
  },
  "analysis_period_days": 7
}
```

### RDS 설정 (`config_rds.json`)

```json
{
  "thresholds": {
    "cpu_low": 20,
    "cpu_high": 80,
    "memory_free_percent_low": 15,
    "iops_utilization_high": 80,
    "replica_lag_ms_high": 1000
  },
  "storage_types": {
    "gp2": {"min_iops": 100, "max_iops": 16000},
    "gp3": {"base_iops": 3000, "max_iops": 16000}
  }
}
```

## IAM Permissions

AWS 계정에 다음 권한이 필요합니다:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "rds:DescribeDBInstances",
        "rds:DescribeDBClusters",
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics"
      ],
      "Resource": "*"
    }
  ]
}
```

전체 IAM 정책은 `iam-policy.json` 파일을 참조하세요.

**정책 적용 방법:**

```bash
# 정책 생성
aws iam create-policy \
  --policy-name AWSResourceAnalyzerPolicy \
  --policy-document file://iam-policy.json

# 사용자에 정책 연결
aws iam attach-user-policy \
  --user-name YOUR_USERNAME \
  --policy-arn arn:aws:iam::ACCOUNT_ID:policy/AWSResourceAnalyzerPolicy
```

또는 AWS 관리형 정책 사용:
```bash
aws iam attach-user-policy \
  --user-name YOUR_USERNAME \
  --policy-arn arn:aws:iam::aws:policy/ReadOnlyAccess
```

## Command Options

### EC2 Analyzer

| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `--region` | AWS 리전 | AWS config 기본값 |
| `--days` | 분석 기간 (일) | 7 |
| `--output-json` | JSON 출력 파일 경로 | - |
| `--output-csv` | CSV 출력 파일 경로 | - |
| `--instance-id` | 특정 인스턴스 ID | - |
| `--tag` | 태그 필터 (Key=Value) | - |

### RDS Analyzer

| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `--region` | AWS 리전 | AWS config 기본값 |
| `--days` | 분석 기간 (일) | 7 |
| `--output-json` | JSON 출력 파일 경로 | - |
| `--output-csv` | CSV 출력 파일 경로 | - |
| `--db-identifier` | 특정 DB 인스턴스 ID | - |
| `--cluster` | 특정 Aurora 클러스터 ID | - |
| `--tag` | 태그 필터 (Key=Value) | - |

## Output Examples

### 콘솔 출력

```
================================================================================
EC2 Instance Analysis Report
================================================================================

Instance Information:
  Instance ID:    i-0abc123def456789
  Name:           web-server-prod
  Current Type:   t3.large
  Analysis Period: Last 7 days

Resource Utilization Metrics:
┌─────────────────┬─────────┬─────────┬─────────┬─────────┐
│ Metric          │ Average │ Maximum │ P95     │ P99     │
├─────────────────┼─────────┼─────────┼─────────┼─────────┤
│ CPU (%)         │ 23.45   │ 68.20   │ 45.10   │ 58.30   │
│ Network In (MB) │ 125.30  │ 450.80  │ 380.20  │ 420.50  │
└─────────────────┴─────────┴─────────┴─────────┴─────────┘

Recommendation:
  Action:         Downsize
  Recommended:    t3.medium
  Risk Level:     Low
  Est. Savings:   ~50%

  Reasons:
    - CPU utilization is low (avg: 23.5%, P95: 45.1%)
================================================================================
```

### JSON 출력

```json
{
  "instance_id": "i-0abc123def456789",
  "current_type": "t3.large",
  "analysis": {
    "cpu": {"average": 23.45, "p95": 45.10, "p99": 58.30}
  },
  "recommendation": {
    "action": "Downsize",
    "recommended_type": "t3.medium",
    "risk_level": "Low",
    "estimated_savings_percent": 50
  }
}
```

## Cost Savings Examples

### EC2 최적화
- **Downsize**: t3.xlarge → t3.large = ~50% 절감 (~$60/월)
- **평균 절감률**: 30-50%

### RDS 최적화
- **gp2 → gp3**: 100GB 기준 ~20% 절감 ($3.50/월)
- **Instance Downsize**: db.r5.xlarge → db.r5.large = ~50% 절감 (~$104/월)
- **Provisioned IOPS 최적화**: io1 → gp3 = ~40% 절감 (사용률이 낮은 경우)

**버전**: 1.0.0  
**최종 업데이트**: 2026-02-04
