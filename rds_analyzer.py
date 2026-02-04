#!/usr/bin/env python3
"""
RDS/Aurora Instance Analyzer
Analyzes RDS/Aurora instance metrics from CloudWatch and recommends optimal instance types and storage configurations.
"""

import json
import csv
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import sys

import boto3
import click
import pandas as pd
import numpy as np
from tabulate import tabulate
from colorama import Fore, Style, init
from botocore.exceptions import ClientError, NoCredentialsError

# Initialize colorama
init(autoreset=True)


class RDSAnalyzer:
    """Main class for RDS/Aurora instance analysis"""
    
    def __init__(self, region: str = None, config_path: str = 'config_rds.json'):
        """Initialize the analyzer with AWS clients and configuration"""
        self.region = region or boto3.Session().region_name or 'us-east-1'
        self.rds_client = boto3.client('rds', region_name=self.region)
        self.cloudwatch_client = boto3.client('cloudwatch', region_name=self.region)
        
        # Load configuration
        try:
            with open(config_path, 'r') as f:
                self.config = json.load(f)
        except FileNotFoundError:
            click.echo(f"{Fore.YELLOW}Warning: Config file not found. Using default settings.{Style.RESET_ALL}")
            self.config = self._default_config()
    
    @staticmethod
    def _default_config() -> Dict:
        """Return default configuration if config file is missing"""
        return {
            "thresholds": {
                "cpu_low": 20,
                "cpu_high": 80,
                "memory_free_percent_low": 15,
                "iops_utilization_high": 80,
                "storage_free_percent_low": 20,
                "connection_percent_high": 80,
                "replica_lag_ms_high": 1000
            },
            "analysis_period_days": 7,
            "recommendation_weights": {
                "cost": 0.4,
                "performance": 0.4,
                "stability": 0.2
            }
        }
    
    def list_db_instances(self, filters: Optional[Dict] = None) -> List[Dict]:
        """List all RDS/Aurora instances"""
        try:
            instances = []
            
            # Get DB instances
            paginator = self.rds_client.get_paginator('describe_db_instances')
            for page in paginator.paginate():
                for db_instance in page['DBInstances']:
                    # Skip instances that are not available
                    if db_instance['DBInstanceStatus'] != 'available':
                        continue
                    
                    # Check if it's Aurora
                    is_aurora = db_instance.get('Engine', '').startswith('aurora')
                    
                    # Get cluster info if Aurora
                    cluster_id = db_instance.get('DBClusterIdentifier', 'N/A')
                    is_cluster_writer = False
                    
                    if is_aurora and cluster_id != 'N/A':
                        try:
                            cluster_response = self.rds_client.describe_db_clusters(
                                DBClusterIdentifier=cluster_id
                            )
                            cluster = cluster_response['DBClusters'][0]
                            is_cluster_writer = db_instance['DBInstanceIdentifier'] in [
                                member['DBInstanceIdentifier'] 
                                for member in cluster.get('DBClusterMembers', []) 
                                if member.get('IsClusterWriter', False)
                            ]
                        except ClientError:
                            pass
                    
                    # Extract tags
                    tags = {}
                    try:
                        tags_response = self.rds_client.list_tags_for_resource(
                            ResourceName=db_instance['DBInstanceArn']
                        )
                        for tag in tags_response.get('TagList', []):
                            tags[tag['Key']] = tag['Value']
                    except ClientError:
                        pass
                    
                    instance_info = {
                        'DBInstanceIdentifier': db_instance['DBInstanceIdentifier'],
                        'DBInstanceClass': db_instance['DBInstanceClass'],
                        'Engine': db_instance['Engine'],
                        'EngineVersion': db_instance['EngineVersion'],
                        'DBInstanceStatus': db_instance['DBInstanceStatus'],
                        'AllocatedStorage': db_instance.get('AllocatedStorage', 0),
                        'StorageType': db_instance.get('StorageType', 'N/A'),
                        'Iops': db_instance.get('Iops', 0),
                        'MultiAZ': db_instance.get('MultiAZ', False),
                        'DBClusterIdentifier': cluster_id,
                        'IsAurora': is_aurora,
                        'IsClusterWriter': is_cluster_writer,
                        'Endpoint': db_instance.get('Endpoint', {}).get('Address', 'N/A'),
                        'AvailabilityZone': db_instance.get('AvailabilityZone', 'N/A'),
                        'Tags': tags
                    }
                    
                    instances.append(instance_info)
            
            return instances
        
        except NoCredentialsError:
            click.echo(f"{Fore.RED}Error: AWS credentials not found. Please configure AWS CLI.{Style.RESET_ALL}")
            sys.exit(1)
        except ClientError as e:
            click.echo(f"{Fore.RED}Error listing RDS instances: {e}{Style.RESET_ALL}")
            sys.exit(1)
    
    def get_metric_statistics(
        self, 
        db_identifier: str, 
        metric_name: str, 
        namespace: str = 'AWS/RDS',
        days: int = 7,
        statistic: str = 'Average',
        unit: Optional[str] = None
    ) -> List[Dict]:
        """Get CloudWatch metric statistics for a DB instance"""
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=days)
        
        # Calculate appropriate period based on days
        if days <= 1:
            period = 300  # 5 minutes
        elif days <= 7:
            period = 3600  # 1 hour
        else:
            period = 86400  # 1 day
        
        try:
            params = {
                'Namespace': namespace,
                'MetricName': metric_name,
                'Dimensions': [{'Name': 'DBInstanceIdentifier', 'Value': db_identifier}],
                'StartTime': start_time,
                'EndTime': end_time,
                'Period': period,
                'Statistics': [statistic]
            }
            
            if unit:
                params['Unit'] = unit
            
            response = self.cloudwatch_client.get_metric_statistics(**params)
            return sorted(response['Datapoints'], key=lambda x: x['Timestamp'])
        
        except ClientError as e:
            click.echo(f"{Fore.YELLOW}Warning: Could not fetch {metric_name}: {e}{Style.RESET_ALL}")
            return []
    
    def collect_all_metrics(self, db_identifier: str, days: int = 7, is_aurora: bool = False) -> Dict:
        """Collect all relevant metrics for a DB instance"""
        click.echo(f"{Fore.CYAN}Collecting metrics for DB instance {db_identifier}...{Style.RESET_ALL}")
        
        metrics = {
            # CPU
            'cpu': self.get_metric_statistics(db_identifier, 'CPUUtilization', days=days),
            
            # Memory
            'freeable_memory': self.get_metric_statistics(
                db_identifier, 'FreeableMemory', days=days, unit='Bytes'
            ),
            'swap_usage': self.get_metric_statistics(
                db_identifier, 'SwapUsage', days=days, unit='Bytes'
            ),
            
            # Storage
            'free_storage_space': self.get_metric_statistics(
                db_identifier, 'FreeStorageSpace', days=days, unit='Bytes'
            ),
            
            # I/O
            'read_iops': self.get_metric_statistics(db_identifier, 'ReadIOPS', days=days),
            'write_iops': self.get_metric_statistics(db_identifier, 'WriteIOPS', days=days),
            'read_latency': self.get_metric_statistics(
                db_identifier, 'ReadLatency', days=days, unit='Seconds'
            ),
            'write_latency': self.get_metric_statistics(
                db_identifier, 'WriteLatency', days=days, unit='Seconds'
            ),
            'read_throughput': self.get_metric_statistics(
                db_identifier, 'ReadThroughput', days=days, unit='Bytes/Second'
            ),
            'write_throughput': self.get_metric_statistics(
                db_identifier, 'WriteThroughput', days=days, unit='Bytes/Second'
            ),
            
            # Connections
            'database_connections': self.get_metric_statistics(
                db_identifier, 'DatabaseConnections', days=days
            ),
            
            # Network
            'network_receive_throughput': self.get_metric_statistics(
                db_identifier, 'NetworkReceiveThroughput', days=days, unit='Bytes/Second'
            ),
            'network_transmit_throughput': self.get_metric_statistics(
                db_identifier, 'NetworkTransmitThroughput', days=days, unit='Bytes/Second'
            ),
            
            # Disk Queue
            'disk_queue_depth': self.get_metric_statistics(
                db_identifier, 'DiskQueueDepth', days=days
            ),
        }
        
        # Aurora-specific metrics
        if is_aurora:
            metrics['aurora_replica_lag'] = self.get_metric_statistics(
                db_identifier, 'AuroraReplicaLag', days=days, unit='Milliseconds'
            )
            metrics['aurora_binlog_replica_lag'] = self.get_metric_statistics(
                db_identifier, 'AuroraBinlogReplicaLag', days=days, unit='Milliseconds'
            )
            metrics['volume_bytes_used'] = self.get_metric_statistics(
                db_identifier, 'VolumeBytesUsed', days=days, unit='Bytes'
            )
        
        return metrics
    
    def calculate_statistics(self, datapoints: List[Dict], metric_key: str = 'Average') -> Dict:
        """Calculate statistics from CloudWatch datapoints"""
        if not datapoints:
            return {
                'average': 0,
                'maximum': 0,
                'minimum': 0,
                'p95': 0,
                'p99': 0,
                'count': 0
            }
        
        values = [dp[metric_key] for dp in datapoints if metric_key in dp]
        
        if not values:
            return {
                'average': 0,
                'maximum': 0,
                'minimum': 0,
                'p95': 0,
                'p99': 0,
                'count': 0
            }
        
        return {
            'average': np.mean(values),
            'maximum': np.max(values),
            'minimum': np.min(values),
            'p95': np.percentile(values, 95),
            'p99': np.percentile(values, 99),
            'count': len(values)
        }
    
    def analyze_metrics(self, metrics: Dict, instance_info: Dict) -> Dict:
        """Analyze collected metrics and return statistics"""
        analysis = {}
        
        # CPU Analysis
        if metrics.get('cpu'):
            analysis['cpu'] = self.calculate_statistics(metrics['cpu'])
        
        # Memory Analysis (convert bytes to GB)
        if metrics.get('freeable_memory'):
            stats = self.calculate_statistics(metrics['freeable_memory'])
            analysis['freeable_memory_gb'] = {k: v / (1024**3) for k, v in stats.items()}
        
        if metrics.get('swap_usage'):
            stats = self.calculate_statistics(metrics['swap_usage'])
            analysis['swap_usage_mb'] = {k: v / (1024**2) for k, v in stats.items()}
        
        # Storage Analysis (convert bytes to GB)
        if metrics.get('free_storage_space'):
            stats = self.calculate_statistics(metrics['free_storage_space'])
            analysis['free_storage_gb'] = {k: v / (1024**3) for k, v in stats.items()}
        
        # IOPS Analysis
        if metrics.get('read_iops'):
            analysis['read_iops'] = self.calculate_statistics(metrics['read_iops'])
        
        if metrics.get('write_iops'):
            analysis['write_iops'] = self.calculate_statistics(metrics['write_iops'])
        
        # Latency Analysis (convert to milliseconds)
        if metrics.get('read_latency'):
            stats = self.calculate_statistics(metrics['read_latency'])
            analysis['read_latency_ms'] = {k: v * 1000 for k, v in stats.items()}
        
        if metrics.get('write_latency'):
            stats = self.calculate_statistics(metrics['write_latency'])
            analysis['write_latency_ms'] = {k: v * 1000 for k, v in stats.items()}
        
        # Throughput Analysis (convert to MB/s)
        if metrics.get('read_throughput'):
            stats = self.calculate_statistics(metrics['read_throughput'])
            analysis['read_throughput_mbps'] = {k: v / (1024**2) for k, v in stats.items()}
        
        if metrics.get('write_throughput'):
            stats = self.calculate_statistics(metrics['write_throughput'])
            analysis['write_throughput_mbps'] = {k: v / (1024**2) for k, v in stats.items()}
        
        # Connections
        if metrics.get('database_connections'):
            analysis['database_connections'] = self.calculate_statistics(metrics['database_connections'])
        
        # Network (convert to MB/s)
        if metrics.get('network_receive_throughput'):
            stats = self.calculate_statistics(metrics['network_receive_throughput'])
            analysis['network_receive_mbps'] = {k: v / (1024**2) for k, v in stats.items()}
        
        if metrics.get('network_transmit_throughput'):
            stats = self.calculate_statistics(metrics['network_transmit_throughput'])
            analysis['network_transmit_mbps'] = {k: v / (1024**2) for k, v in stats.items()}
        
        # Disk Queue
        if metrics.get('disk_queue_depth'):
            analysis['disk_queue_depth'] = self.calculate_statistics(metrics['disk_queue_depth'])
        
        # Aurora-specific
        if metrics.get('aurora_replica_lag'):
            analysis['aurora_replica_lag_ms'] = self.calculate_statistics(metrics['aurora_replica_lag'])
        
        if metrics.get('volume_bytes_used'):
            stats = self.calculate_statistics(metrics['volume_bytes_used'])
            analysis['volume_bytes_used_gb'] = {k: v / (1024**3) for k, v in stats.items()}
        
        return analysis
    
    def get_instance_specs(self, instance_class: str) -> Dict:
        """Get specifications for a DB instance class"""
        # Simplified instance specs database
        specs_db = {
            # T3 family
            'db.t3.micro': {'vcpu': 2, 'memory_gb': 1, 'network_performance': 'Low to Moderate'},
            'db.t3.small': {'vcpu': 2, 'memory_gb': 2, 'network_performance': 'Low to Moderate'},
            'db.t3.medium': {'vcpu': 2, 'memory_gb': 4, 'network_performance': 'Low to Moderate'},
            'db.t3.large': {'vcpu': 2, 'memory_gb': 8, 'network_performance': 'Moderate'},
            'db.t3.xlarge': {'vcpu': 4, 'memory_gb': 16, 'network_performance': 'Moderate'},
            'db.t3.2xlarge': {'vcpu': 8, 'memory_gb': 32, 'network_performance': 'Moderate'},
            
            # T4g family (Graviton2)
            'db.t4g.micro': {'vcpu': 2, 'memory_gb': 1, 'network_performance': 'Up to 5 Gigabit'},
            'db.t4g.small': {'vcpu': 2, 'memory_gb': 2, 'network_performance': 'Up to 5 Gigabit'},
            'db.t4g.medium': {'vcpu': 2, 'memory_gb': 4, 'network_performance': 'Up to 5 Gigabit'},
            'db.t4g.large': {'vcpu': 2, 'memory_gb': 8, 'network_performance': 'Up to 5 Gigabit'},
            
            # M5 family
            'db.m5.large': {'vcpu': 2, 'memory_gb': 8, 'network_performance': 'Up to 10 Gigabit'},
            'db.m5.xlarge': {'vcpu': 4, 'memory_gb': 16, 'network_performance': 'Up to 10 Gigabit'},
            'db.m5.2xlarge': {'vcpu': 8, 'memory_gb': 32, 'network_performance': 'Up to 10 Gigabit'},
            'db.m5.4xlarge': {'vcpu': 16, 'memory_gb': 64, 'network_performance': '10 Gigabit'},
            'db.m5.8xlarge': {'vcpu': 32, 'memory_gb': 128, 'network_performance': '10 Gigabit'},
            'db.m5.12xlarge': {'vcpu': 48, 'memory_gb': 192, 'network_performance': '12 Gigabit'},
            'db.m5.16xlarge': {'vcpu': 64, 'memory_gb': 256, 'network_performance': '20 Gigabit'},
            'db.m5.24xlarge': {'vcpu': 96, 'memory_gb': 384, 'network_performance': '25 Gigabit'},
            
            # R5 family (Memory optimized)
            'db.r5.large': {'vcpu': 2, 'memory_gb': 16, 'network_performance': 'Up to 10 Gigabit'},
            'db.r5.xlarge': {'vcpu': 4, 'memory_gb': 32, 'network_performance': 'Up to 10 Gigabit'},
            'db.r5.2xlarge': {'vcpu': 8, 'memory_gb': 64, 'network_performance': 'Up to 10 Gigabit'},
            'db.r5.4xlarge': {'vcpu': 16, 'memory_gb': 128, 'network_performance': '10 Gigabit'},
            'db.r5.8xlarge': {'vcpu': 32, 'memory_gb': 256, 'network_performance': '10 Gigabit'},
            'db.r5.12xlarge': {'vcpu': 48, 'memory_gb': 384, 'network_performance': '12 Gigabit'},
            'db.r5.16xlarge': {'vcpu': 64, 'memory_gb': 512, 'network_performance': '20 Gigabit'},
            'db.r5.24xlarge': {'vcpu': 96, 'memory_gb': 768, 'network_performance': '25 Gigabit'},
            
            # R6g family (Graviton2)
            'db.r6g.large': {'vcpu': 2, 'memory_gb': 16, 'network_performance': 'Up to 10 Gigabit'},
            'db.r6g.xlarge': {'vcpu': 4, 'memory_gb': 32, 'network_performance': 'Up to 10 Gigabit'},
            'db.r6g.2xlarge': {'vcpu': 8, 'memory_gb': 64, 'network_performance': 'Up to 10 Gigabit'},
            'db.r6g.4xlarge': {'vcpu': 16, 'memory_gb': 128, 'network_performance': 'Up to 10 Gigabit'},
            'db.r6g.8xlarge': {'vcpu': 32, 'memory_gb': 256, 'network_performance': '12 Gigabit'},
            'db.r6g.12xlarge': {'vcpu': 48, 'memory_gb': 384, 'network_performance': '20 Gigabit'},
            'db.r6g.16xlarge': {'vcpu': 64, 'memory_gb': 512, 'network_performance': '25 Gigabit'},
        }
        
        return specs_db.get(instance_class, {
            'vcpu': 0,
            'memory_gb': 0,
            'network_performance': 'Unknown'
        })
    
    def recommend_instance_class(
        self, 
        current_class: str, 
        analysis: Dict,
        instance_info: Dict
    ) -> Dict:
        """Recommend optimal instance class based on analysis"""
        current_specs = self.get_instance_specs(current_class)
        cpu_stats = analysis.get('cpu', {})
        
        recommendation = {
            'current_class': current_class,
            'recommended_class': current_class,
            'action': 'Keep current',
            'reason': [],
            'risk_level': 'Low',
            'estimated_savings_percent': 0,
            'confidence': 'High'
        }
        
        # CPU analysis
        cpu_avg = cpu_stats.get('average', 0)
        cpu_p95 = cpu_stats.get('p95', 0)
        cpu_max = cpu_stats.get('maximum', 0)
        
        thresholds = self.config['thresholds']
        
        # Memory analysis
        memory_stats = analysis.get('freeable_memory_gb', {})
        total_memory = current_specs.get('memory_gb', 0)
        
        if total_memory > 0 and memory_stats:
            free_memory_avg = memory_stats.get('average', 0)
            memory_usage_percent = ((total_memory - free_memory_avg) / total_memory) * 100
            
            # Check for memory pressure
            if memory_usage_percent > (100 - thresholds['memory_free_percent_low']):
                recommendation['reason'].append(
                    f"High memory usage ({memory_usage_percent:.1f}%), consider memory-optimized instance"
                )
                if recommendation['action'] == 'Keep current':
                    recommendation['action'] = 'Upsize or change to R family'
                    recommendation['risk_level'] = 'High'
        
        # Swap usage check
        swap_stats = analysis.get('swap_usage_mb', {})
        if swap_stats and swap_stats.get('average', 0) > 300:
            recommendation['reason'].append(
                f"Swap usage detected ({swap_stats['average']:.1f} MB avg), severe memory pressure"
            )
            recommendation['action'] = 'Upsize immediately'
            recommendation['risk_level'] = 'Critical'
        
        # CPU-based recommendations
        used_memory_gb = current_specs.get('memory_gb', 0) - analysis.get('freeable_memory_gb', {}).get('average', 0)
        target_memory_gb = current_specs.get('memory_gb', 0) / 2

        if cpu_avg < thresholds['cpu_low'] and used_memory_gb < (target_memory_gb * 0.8):
            recommendation['reason'].append(
                f"CPU utilization is low (avg: {cpu_avg:.1f}%, P95: {cpu_p95:.1f}%)"
            )
            if recommendation['action'] == 'Keep current':
                recommendation['action'] = 'Downsize'
                
                # Suggest smaller instance
                if current_class.endswith('.24xlarge'):
                    recommendation['recommended_class'] = current_class.replace('.24xlarge', '.16xlarge')
                    recommendation['estimated_savings_percent'] = 33
                elif current_class.endswith('.16xlarge'):
                    recommendation['recommended_class'] = current_class.replace('.16xlarge', '.12xlarge')
                    recommendation['estimated_savings_percent'] = 25
                elif current_class.endswith('.12xlarge'):
                    recommendation['recommended_class'] = current_class.replace('.12xlarge', '.8xlarge')
                    recommendation['estimated_savings_percent'] = 33
                elif current_class.endswith('.8xlarge'):
                    recommendation['recommended_class'] = current_class.replace('.8xlarge', '.4xlarge')
                    recommendation['estimated_savings_percent'] = 50
                elif current_class.endswith('.4xlarge'):
                    recommendation['recommended_class'] = current_class.replace('.4xlarge', '.2xlarge')
                    recommendation['estimated_savings_percent'] = 50
                elif current_class.endswith('.2xlarge'):
                    recommendation['recommended_class'] = current_class.replace('.2xlarge', '.xlarge')
                    recommendation['estimated_savings_percent'] = 50
                elif current_class.endswith('.xlarge') and not current_class.endswith('.2xlarge'):
                    recommendation['recommended_class'] = current_class.replace('.xlarge', '.large')
                    recommendation['estimated_savings_percent'] = 50
                elif current_class.endswith('.large'):
                    recommendation['recommended_class'] = current_class.replace('.large', '.medium')
                    recommendation['estimated_savings_percent'] = 50
        
        elif cpu_p95 > thresholds['cpu_high'] or cpu_max > 90:
            recommendation['reason'].append(
                f"CPU utilization is high (P95: {cpu_p95:.1f}%, Max: {cpu_max:.1f}%)"
            )
            if 'Upsize' not in recommendation['action']:
                recommendation['action'] = 'Upsize'
                recommendation['risk_level'] = 'High'
                
                # Suggest larger instance
                if current_class.endswith('.medium'):
                    recommendation['recommended_class'] = current_class.replace('.medium', '.large')
                elif current_class.endswith('.large') and not current_class.endswith('.xlarge'):
                    recommendation['recommended_class'] = current_class.replace('.large', '.xlarge')
                elif current_class.endswith('.xlarge') and not current_class.endswith('.2xlarge'):
                    recommendation['recommended_class'] = current_class.replace('.xlarge', '.2xlarge')
                elif current_class.endswith('.2xlarge'):
                    recommendation['recommended_class'] = current_class.replace('.2xlarge', '.4xlarge')
                elif current_class.endswith('.4xlarge'):
                    recommendation['recommended_class'] = current_class.replace('.4xlarge', '.8xlarge')
        
        # If no reason was added, instance is well-sized
        if not recommendation['reason']:
            recommendation['reason'].append("Instance is appropriately sized for current workload")
        
        return recommendation
    
    def recommend_storage(self, analysis: Dict, instance_info: Dict) -> Dict:
        """Recommend storage type and configuration"""
        current_storage_type = instance_info.get('StorageType', 'gp2')
        allocated_storage = instance_info.get('AllocatedStorage', 0)
        current_iops = instance_info.get('Iops', 0)
        
        recommendation = {
            'current_type': current_storage_type,
            'recommended_type': current_storage_type,
            'action': 'Keep current',
            'reason': [],
            'estimated_savings_percent': 0
        }
        
        # Aurora uses cluster storage, different recommendations
        if instance_info.get('IsAurora', False):
            recommendation['reason'].append("Aurora uses cluster-level storage (automatic scaling)")
            
            # Check volume usage
            volume_stats = analysis.get('volume_bytes_used_gb', {})
            if volume_stats:
                avg_usage = volume_stats.get('average', 0)
                recommendation['reason'].append(f"Average storage usage: {avg_usage:.1f} GB")
            
            return recommendation
        
        # Standard RDS storage recommendations
        free_storage_stats = analysis.get('free_storage_gb', {})
        
        if free_storage_stats:
            free_storage = free_storage_stats.get('average', 0)
            storage_usage_percent = ((allocated_storage - free_storage) / allocated_storage) * 100 if allocated_storage > 0 else 0
            
            thresholds = self.config['thresholds']
            
            if storage_usage_percent > (100 - thresholds['storage_free_percent_low']):
                recommendation['reason'].append(
                    f"Low free storage ({free_storage:.1f} GB free, {storage_usage_percent:.1f}% used)"
                )
                recommendation['action'] = 'Increase storage size'
        
        # IOPS analysis
        read_iops_stats = analysis.get('read_iops', {})
        write_iops_stats = analysis.get('write_iops', {})
        
        if read_iops_stats and write_iops_stats:
            total_iops_avg = read_iops_stats.get('average', 0) + write_iops_stats.get('average', 0)
            total_iops_p95 = read_iops_stats.get('p95', 0) + write_iops_stats.get('p95', 0)
            
            # gp2 to gp3 conversion recommendation
            if current_storage_type == 'gp2':
                gp3_base_iops = self.config['storage_types']['gp3']['base_iops']
                
                if total_iops_p95 < gp3_base_iops:
                    recommendation['recommended_type'] = 'gp3'
                    recommendation['action'] = 'Convert to gp3'
                    recommendation['estimated_savings_percent'] = 20
                    recommendation['reason'].append(
                        f"IOPS usage ({total_iops_p95:.0f}) below gp3 baseline (3000), can save ~20% with gp3"
                    )
                elif total_iops_p95 > 10000:
                    recommendation['recommended_type'] = 'io1 or io2'
                    recommendation['action'] = 'Consider Provisioned IOPS'
                    recommendation['reason'].append(
                        f"High IOPS demand ({total_iops_p95:.0f}), Provisioned IOPS may provide better performance"
                    )
            
            # Check if current Provisioned IOPS is underutilized
            if current_storage_type in ['io1', 'io2'] and current_iops > 0:
                iops_utilization = (total_iops_p95 / current_iops) * 100
                
                if iops_utilization < 20:
                    recommendation['recommended_type'] = 'gp3'
                    recommendation['action'] = 'Downgrade to gp3'
                    recommendation['estimated_savings_percent'] = 40
                    recommendation['reason'].append(
                        f"Provisioned IOPS underutilized ({iops_utilization:.1f}%), gp3 may be sufficient"
                    )
        
        if not recommendation['reason']:
            recommendation['reason'].append("Storage configuration is appropriate")
        
        return recommendation
    
    def analyze_read_replica_need(self, analysis: Dict, instance_info: Dict) -> Dict:
        """Analyze if read replicas are needed or if existing replicas are sufficient"""
        recommendation = {
            'action': 'No action needed',
            'reason': [],
            'recommended_replicas': 0
        }
        
        # Only for cluster writers or standalone instances
        if instance_info.get('IsClusterWriter', False) or not instance_info.get('IsAurora', False):
            read_iops = analysis.get('read_iops', {}).get('average', 0)
            write_iops = analysis.get('write_iops', {}).get('average', 0)
            
            if write_iops > 0:
                read_write_ratio = read_iops / write_iops
                
                if read_write_ratio > 3.0:
                    recommendation['action'] = 'Consider adding read replicas'
                    recommendation['recommended_replicas'] = 2 if read_write_ratio > 5 else 1
                    recommendation['reason'].append(
                        f"Read-heavy workload (R/W ratio: {read_write_ratio:.1f})"
                    )
            
            connections = analysis.get('database_connections', {}).get('average', 0)
            if connections > 100:
                recommendation['reason'].append(
                    f"High connection count ({connections:.0f} avg)"
                )
                if recommendation['recommended_replicas'] == 0:
                    recommendation['recommended_replicas'] = 1
        
        # Check replica lag for existing replicas
        if not instance_info.get('IsClusterWriter', True):
            replica_lag = analysis.get('aurora_replica_lag_ms', {})
            if replica_lag:
                lag_avg = replica_lag.get('average', 0)
                lag_p95 = replica_lag.get('p95', 0)
                
                thresholds = self.config.get('aurora_specific', {})
                lag_critical = thresholds.get('replica_lag_critical_ms', 1000)
                
                if lag_p95 > lag_critical:
                    recommendation['action'] = 'Upsize replica instance'
                    recommendation['reason'].append(
                        f"High replica lag (P95: {lag_p95:.0f}ms)"
                    )
        
        if not recommendation['reason']:
            recommendation['reason'].append("Current configuration is appropriate")
        
        return recommendation


def display_instances_table(instances: List[Dict]) -> None:
    """Display DB instances in a formatted table"""
    if not instances:
        click.echo(f"{Fore.YELLOW}No DB instances found.{Style.RESET_ALL}")
        return
    
    headers = ['#', 'DB Identifier', 'Engine', 'Instance Class', 'Storage', 'Role', 'Status']
    rows = []
    
    for idx, inst in enumerate(instances, 1):
        # Determine role
        if inst.get('IsAurora', False):
            if inst.get('IsClusterWriter', False):
                role = 'Writer'
            else:
                role = 'Reader'
        else:
            role = 'Standalone'
        
        # Storage info
        storage_info = f"{inst.get('StorageType', 'N/A')}"
        if inst.get('AllocatedStorage', 0) > 0:
            storage_info += f" ({inst['AllocatedStorage']}GB)"
        
        rows.append([
            idx,
            inst['DBInstanceIdentifier'][:40],
            f"{inst.get('Engine', 'N/A')}",
            inst.get('DBInstanceClass', 'N/A'),
            storage_info,
            role,
            inst.get('DBInstanceStatus', 'N/A')
        ])
    
    click.echo(f"\n{Fore.CYAN}Available RDS/Aurora Instances:{Style.RESET_ALL}")
    click.echo(tabulate(rows, headers=headers, tablefmt='grid'))


def display_analysis_report(
    instance: Dict, 
    analysis: Dict, 
    instance_recommendation: Dict,
    storage_recommendation: Dict,
    replica_recommendation: Dict,
    days: int
) -> None:
    """Display the analysis report in a formatted way"""
    click.echo(f"\n{'=' * 80}")
    click.echo(f"{Fore.GREEN}{Style.BRIGHT}RDS/Aurora Instance Analysis Report{Style.RESET_ALL}")
    click.echo(f"{'=' * 80}")
    
    # Instance Information
    click.echo(f"\n{Fore.CYAN}Instance Information:{Style.RESET_ALL}")
    click.echo(f"  DB Identifier:  {instance['DBInstanceIdentifier']}")
    click.echo(f"  Engine:         {instance['Engine']} {instance.get('EngineVersion', '')}")
    click.echo(f"  Current Class:  {instance['DBInstanceClass']}")
    click.echo(f"  Storage:        {instance.get('StorageType', 'N/A')} ({instance.get('AllocatedStorage', 0)} GB)")
    click.echo(f"  Multi-AZ:       {instance.get('MultiAZ', False)}")
    
    if instance.get('IsAurora', False):
        role = "Writer" if instance.get('IsClusterWriter', False) else "Reader"
        click.echo(f"  Aurora Role:    {role}")
        click.echo(f"  Cluster:        {instance.get('DBClusterIdentifier', 'N/A')}")
    
    click.echo(f"  Analysis Period: Last {days} days")
    
    # Metrics Table
    click.echo(f"\n{Fore.CYAN}Resource Utilization Metrics:{Style.RESET_ALL}")
    
    metrics_data = []
    
    # CPU
    if 'cpu' in analysis:
        cpu = analysis['cpu']
        metrics_data.append([
            'CPU (%)',
            f"{cpu['average']:.2f}",
            f"{cpu['maximum']:.2f}",
            f"{cpu['p95']:.2f}",
            f"{cpu['p99']:.2f}"
        ])
    
    # Memory
    if 'freeable_memory_gb' in analysis:
        mem = analysis['freeable_memory_gb']
        metrics_data.append([
            'Free Memory (GB)',
            f"{mem['average']:.2f}",
            f"{mem['minimum']:.2f}",
            f"{mem['p95']:.2f}",
            f"{mem['p99']:.2f}"
        ])
    
    # Connections
    if 'database_connections' in analysis:
        conn = analysis['database_connections']
        metrics_data.append([
            'DB Connections',
            f"{conn['average']:.0f}",
            f"{conn['maximum']:.0f}",
            f"{conn['p95']:.0f}",
            f"{conn['p99']:.0f}"
        ])
    
    # Read IOPS
    if 'read_iops' in analysis:
        riops = analysis['read_iops']
        metrics_data.append([
            'Read IOPS',
            f"{riops['average']:.0f}",
            f"{riops['maximum']:.0f}",
            f"{riops['p95']:.0f}",
            f"{riops['p99']:.0f}"
        ])
    
    # Write IOPS
    if 'write_iops' in analysis:
        wiops = analysis['write_iops']
        metrics_data.append([
            'Write IOPS',
            f"{wiops['average']:.0f}",
            f"{wiops['maximum']:.0f}",
            f"{wiops['p95']:.0f}",
            f"{wiops['p99']:.0f}"
        ])
    
    # Read Latency
    if 'read_latency_ms' in analysis:
        rlat = analysis['read_latency_ms']
        metrics_data.append([
            'Read Latency (ms)',
            f"{rlat['average']:.2f}",
            f"{rlat['maximum']:.2f}",
            f"{rlat['p95']:.2f}",
            f"{rlat['p99']:.2f}"
        ])
    
    # Write Latency
    if 'write_latency_ms' in analysis:
        wlat = analysis['write_latency_ms']
        metrics_data.append([
            'Write Latency (ms)',
            f"{wlat['average']:.2f}",
            f"{wlat['maximum']:.2f}",
            f"{wlat['p95']:.2f}",
            f"{wlat['p99']:.2f}"
        ])
    
    # Aurora Replica Lag
    if 'aurora_replica_lag_ms' in analysis:
        lag = analysis['aurora_replica_lag_ms']
        metrics_data.append([
            'Replica Lag (ms)',
            f"{lag['average']:.0f}",
            f"{lag['maximum']:.0f}",
            f"{lag['p95']:.0f}",
            f"{lag['p99']:.0f}"
        ])
    
    headers = ['Metric', 'Average', 'Maximum', 'P95', 'P99']
    click.echo(tabulate(metrics_data, headers=headers, tablefmt='grid'))
    
    # Instance Recommendation
    click.echo(f"\n{Fore.CYAN}Instance Class Recommendation:{Style.RESET_ALL}")
    
    action_color = {
        'Keep current': Fore.GREEN,
        'Downsize': Fore.YELLOW,
        'Upsize': Fore.RED,
        'Upsize immediately': Fore.RED,
        'Upsize or change to R family': Fore.RED
    }.get(instance_recommendation['action'], Fore.WHITE)
    
    risk_color = {
        'Low': Fore.GREEN,
        'Medium': Fore.YELLOW,
        'High': Fore.RED,
        'Critical': Fore.RED + Style.BRIGHT
    }.get(instance_recommendation['risk_level'], Fore.WHITE)
    
    click.echo(f"  Action:         {action_color}{instance_recommendation['action']}{Style.RESET_ALL}")
    click.echo(f"  Recommended:    {Fore.GREEN}{instance_recommendation['recommended_class']}{Style.RESET_ALL}")
    click.echo(f"  Risk Level:     {risk_color}{instance_recommendation['risk_level']}{Style.RESET_ALL}")
    click.echo(f"  Confidence:     {instance_recommendation['confidence']}")
    
    if instance_recommendation['estimated_savings_percent'] > 0:
        click.echo(f"  Est. Savings:   {Fore.GREEN}~{instance_recommendation['estimated_savings_percent']}%{Style.RESET_ALL}")
    
    click.echo(f"\n  Reasons:")
    for reason in instance_recommendation['reason']:
        click.echo(f"    - {reason}")
    
    # Storage Recommendation
    click.echo(f"\n{Fore.CYAN}Storage Recommendation:{Style.RESET_ALL}")
    click.echo(f"  Current:        {storage_recommendation['current_type']}")
    click.echo(f"  Recommended:    {storage_recommendation['recommended_type']}")
    click.echo(f"  Action:         {storage_recommendation['action']}")
    
    if storage_recommendation['estimated_savings_percent'] > 0:
        click.echo(f"  Est. Savings:   {Fore.GREEN}~{storage_recommendation['estimated_savings_percent']}%{Style.RESET_ALL}")
    
    click.echo(f"\n  Reasons:")
    for reason in storage_recommendation['reason']:
        click.echo(f"    - {reason}")
    
    # Read Replica Recommendation
    if replica_recommendation['action'] != 'No action needed':
        click.echo(f"\n{Fore.CYAN}Read Replica Recommendation:{Style.RESET_ALL}")
        click.echo(f"  Action:         {replica_recommendation['action']}")
        if replica_recommendation['recommended_replicas'] > 0:
            click.echo(f"  Recommended:    {replica_recommendation['recommended_replicas']} replica(s)")
        
        click.echo(f"\n  Reasons:")
        for reason in replica_recommendation['reason']:
            click.echo(f"    - {reason}")
    
    click.echo(f"\n{'=' * 80}\n")


@click.group()
def cli():
    """RDS/Aurora Instance Analyzer - Analyze and optimize your RDS/Aurora instances"""
    pass


@cli.command()
@click.option('--region', default=None, help='AWS region (default: from AWS config)')
@click.option('--days', default=7, help='Number of days to analyze (default: 7)')
@click.option('--output-json', type=click.Path(), help='Save results to JSON file')
@click.option('--output-csv', type=click.Path(), help='Save results to CSV file')
@click.option('--db-identifier', help='Analyze specific DB instance identifier')
@click.option('--tag', help='Filter by tag (format: Key=Value)')
@click.option('--cluster', help='Analyze all instances in a specific cluster')
def analyze(region, days, output_json, output_csv, db_identifier, tag, cluster):
    """Analyze RDS/Aurora instances and get recommendations"""
    
    click.echo(f"{Fore.GREEN}{Style.BRIGHT}RDS/Aurora Instance Analyzer{Style.RESET_ALL}")
    click.echo(f"Region: {region or 'default'}\n")
    
    analyzer = RDSAnalyzer(region=region)
    
    # Get instances
    instances = analyzer.list_db_instances()
    
    if not instances:
        click.echo(f"{Fore.YELLOW}No available DB instances found.{Style.RESET_ALL}")
        return
    
    # Filter by cluster if specified
    if cluster:
        instances = [i for i in instances if i.get('DBClusterIdentifier') == cluster]
        if not instances:
            click.echo(f"{Fore.YELLOW}No instances found in cluster {cluster}.{Style.RESET_ALL}")
            return
    
    # Filter by db_identifier if specified
    if db_identifier:
        instances = [i for i in instances if i['DBInstanceIdentifier'] == db_identifier]
        if not instances:
            click.echo(f"{Fore.YELLOW}DB instance {db_identifier} not found.{Style.RESET_ALL}")
            return
        selected_instances = instances
    else:
        # Display instances and let user select
        display_instances_table(instances)
        
        click.echo(f"\n{Fore.CYAN}Select instances to analyze:{Style.RESET_ALL}")
        click.echo("  Enter instance numbers separated by commas (e.g., 1,3,5)")
        click.echo("  Or enter 'all' to analyze all instances")
        
        selection = click.prompt("Your selection", type=str, default="all")
        
        if selection.lower() == 'all':
            selected_instances = instances
        else:
            indices = [int(i.strip()) - 1 for i in selection.split(',')]
            selected_instances = [instances[i] for i in indices if 0 <= i < len(instances)]
    
    # Analyze each selected instance
    results = []
    
    for instance in selected_instances:
        click.echo(f"\n{Fore.YELLOW}Analyzing {instance['DBInstanceIdentifier']} ({instance['Engine']})...{Style.RESET_ALL}")
        
        # Collect metrics
        metrics = analyzer.collect_all_metrics(
            instance['DBInstanceIdentifier'], 
            days=days,
            is_aurora=instance.get('IsAurora', False)
        )
        
        # Analyze metrics
        analysis = analyzer.analyze_metrics(metrics, instance)
        
        # Get recommendations
        instance_recommendation = analyzer.recommend_instance_class(
            instance['DBInstanceClass'],
            analysis,
            instance
        )
        
        storage_recommendation = analyzer.recommend_storage(analysis, instance)
        
        replica_recommendation = analyzer.analyze_read_replica_need(analysis, instance)
        
        # Display report
        display_analysis_report(
            instance, 
            analysis, 
            instance_recommendation,
            storage_recommendation,
            replica_recommendation,
            days
        )
        
        # Store results
        results.append({
            'instance': instance,
            'analysis': analysis,
            'instance_recommendation': instance_recommendation,
            'storage_recommendation': storage_recommendation,
            'replica_recommendation': replica_recommendation
        })
    
    # Save to files if requested
    if output_json:
        save_json_report(results, output_json)
        click.echo(f"{Fore.GREEN}JSON report saved to: {output_json}{Style.RESET_ALL}")
    
    if output_csv:
        save_csv_report(results, output_csv)
        click.echo(f"{Fore.GREEN}CSV report saved to: {output_csv}{Style.RESET_ALL}")


def save_json_report(results: List[Dict], filepath: str) -> None:
    """Save analysis results to JSON file"""
    output = []
    
    for result in results:
        instance = result['instance']
        analysis = result['analysis']
        inst_rec = result['instance_recommendation']
        stor_rec = result['storage_recommendation']
        repl_rec = result['replica_recommendation']
        
        output.append({
            'db_instance_identifier': instance['DBInstanceIdentifier'],
            'db_engine': instance['Engine'],
            'current_instance_class': instance['DBInstanceClass'],
            'is_aurora': instance.get('IsAurora', False),
            'is_cluster_writer': instance.get('IsClusterWriter', False),
            'cluster_identifier': instance.get('DBClusterIdentifier', 'N/A'),
            'storage_type': instance.get('StorageType', 'N/A'),
            'allocated_storage': instance.get('AllocatedStorage', 0),
            'multi_az': instance.get('MultiAZ', False),
            'analysis': analysis,
            'instance_recommendation': inst_rec,
            'storage_recommendation': stor_rec,
            'replica_recommendation': repl_rec,
            'timestamp': datetime.utcnow().isoformat()
        })
    
    with open(filepath, 'w') as f:
        json.dump(output, f, indent=2, default=str)


def save_csv_report(results: List[Dict], filepath: str) -> None:
    """Save analysis results to CSV file"""
    with open(filepath, 'w', newline='') as f:
        writer = csv.writer(f)
        
        # Write header
        writer.writerow([
            'DB Identifier', 'Engine', 'Current Class', 'Storage Type',
            'CPU Avg (%)', 'CPU P95 (%)', 'CPU Max (%)',
            'Free Memory Avg (GB)', 'Connections Avg',
            'Read IOPS Avg', 'Write IOPS Avg',
            'Read Latency P95 (ms)', 'Write Latency P95 (ms)',
            'Instance Action', 'Recommended Class', 'Risk Level', 
            'Est. Savings (%)', 'Storage Action', 'Recommended Storage',
            'Replica Action', 'Recommended Replicas'
        ])
        
        # Write data
        for result in results:
            instance = result['instance']
            analysis = result['analysis']
            inst_rec = result['instance_recommendation']
            stor_rec = result['storage_recommendation']
            repl_rec = result['replica_recommendation']
            
            cpu = analysis.get('cpu', {})
            mem = analysis.get('freeable_memory_gb', {})
            conn = analysis.get('database_connections', {})
            riops = analysis.get('read_iops', {})
            wiops = analysis.get('write_iops', {})
            rlat = analysis.get('read_latency_ms', {})
            wlat = analysis.get('write_latency_ms', {})
            
            writer.writerow([
                instance['DBInstanceIdentifier'],
                instance['Engine'],
                instance['DBInstanceClass'],
                instance.get('StorageType', 'N/A'),
                f"{cpu.get('average', 0):.2f}",
                f"{cpu.get('p95', 0):.2f}",
                f"{cpu.get('maximum', 0):.2f}",
                f"{mem.get('average', 0):.2f}",
                f"{conn.get('average', 0):.0f}",
                f"{riops.get('average', 0):.0f}",
                f"{wiops.get('average', 0):.0f}",
                f"{rlat.get('p95', 0):.2f}",
                f"{wlat.get('p95', 0):.2f}",
                inst_rec['action'],
                inst_rec['recommended_class'],
                inst_rec['risk_level'],
                inst_rec['estimated_savings_percent'],
                stor_rec['action'],
                stor_rec['recommended_type'],
                repl_rec['action'],
                repl_rec.get('recommended_replicas', 0)
            ])


@cli.command()
@click.option('--region', default=None, help='AWS region (default: from AWS config)')
def list_instances_cmd(region):
    """List all available RDS/Aurora instances"""
    analyzer = RDSAnalyzer(region=region)
    instances = analyzer.list_db_instances()
    display_instances_table(instances)


@cli.command()
@click.option('--region', default=None, help='AWS region (default: from AWS config)')
def list_clusters(region):
    """List all Aurora clusters"""
    analyzer = RDSAnalyzer(region=region)
    
    try:
        response = analyzer.rds_client.describe_db_clusters()
        clusters = response['DBClusters']
        
        if not clusters:
            click.echo(f"{Fore.YELLOW}No Aurora clusters found.{Style.RESET_ALL}")
            return
        
        headers = ['Cluster Identifier', 'Engine', 'Status', 'Members', 'Writer', 'Readers']
        rows = []
        
        for cluster in clusters:
            members = cluster.get('DBClusterMembers', [])
            writer = [m['DBInstanceIdentifier'] for m in members if m.get('IsClusterWriter', False)]
            readers = [m['DBInstanceIdentifier'] for m in members if not m.get('IsClusterWriter', True)]
            
            rows.append([
                cluster['DBClusterIdentifier'],
                cluster['Engine'],
                cluster['Status'],
                len(members),
                writer[0] if writer else 'N/A',
                ', '.join(readers[:2]) + ('...' if len(readers) > 2 else '')
            ])
        
        click.echo(f"\n{Fore.CYAN}Aurora Clusters:{Style.RESET_ALL}")
        click.echo(tabulate(rows, headers=headers, tablefmt='grid'))
        
    except ClientError as e:
        click.echo(f"{Fore.RED}Error listing clusters: {e}{Style.RESET_ALL}")


if __name__ == '__main__':
    cli()
