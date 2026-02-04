#!/usr/bin/env python3
"""
EC2 Instance Analyzer
Analyzes EC2 instance metrics from CloudWatch and recommends optimal instance types.
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


class EC2Analyzer:
    """Main class for EC2 instance analysis"""
    
    def __init__(self, region: str = None, config_path: str = 'config.json'):
        """Initialize the analyzer with AWS clients and configuration"""
        self.region = region or boto3.Session().region_name or 'us-east-1'
        self.ec2_client = boto3.client('ec2', region_name=self.region)
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
                "network_high_mbps": 1000,
                "memory_high": 85
            },
            "analysis_period_days": 7,
            "recommendation_weights": {
                "cost": 0.4,
                "performance": 0.4,
                "stability": 0.2
            }
        }
    
    def list_instances(self, filters: Optional[List[Dict]] = None) -> List[Dict]:
        """List all running EC2 instances with optional filters"""
        try:
            if filters is None:
                filters = [{'Name': 'instance-state-name', 'Values': ['running']}]
            
            response = self.ec2_client.describe_instances(Filters=filters)
            
            instances = []
            for reservation in response['Reservations']:
                for instance in reservation['Instances']:
                    # Extract name tag
                    name = 'N/A'
                    for tag in instance.get('Tags', []):
                        if tag['Key'] == 'Name':
                            name = tag['Value']
                            break
                    
                    instances.append({
                        'InstanceId': instance['InstanceId'],
                        'InstanceType': instance['InstanceType'],
                        'Name': name,
                        'LaunchTime': instance['LaunchTime'],
                        'PrivateIpAddress': instance.get('PrivateIpAddress', 'N/A'),
                        'State': instance['State']['Name']
                    })
            
            return instances
        
        except NoCredentialsError:
            click.echo(f"{Fore.RED}Error: AWS credentials not found. Please configure AWS CLI.{Style.RESET_ALL}")
            sys.exit(1)
        except ClientError as e:
            click.echo(f"{Fore.RED}Error listing instances: {e}{Style.RESET_ALL}")
            sys.exit(1)
    
    def get_metric_statistics(
        self, 
        instance_id: str, 
        metric_name: str, 
        namespace: str = 'AWS/EC2',
        days: int = 7,
        statistic: str = 'Average',
        unit: Optional[str] = None
    ) -> List[Dict]:
        """Get CloudWatch metric statistics for an instance"""
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=days)
        
        # Calculate appropriate period based on days
        # CloudWatch allows max 1440 data points
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
                'Dimensions': [{'Name': 'InstanceId', 'Value': instance_id}],
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
    
    def collect_all_metrics(self, instance_id: str, days: int = 7) -> Dict:
        """Collect all relevant metrics for an instance"""
        click.echo(f"{Fore.CYAN}Collecting metrics for instance {instance_id}...{Style.RESET_ALL}")
        
        metrics = {
            'cpu': self.get_metric_statistics(instance_id, 'CPUUtilization', days=days),
            'network_in': self.get_metric_statistics(instance_id, 'NetworkIn', days=days, unit='Bytes'),
            'network_out': self.get_metric_statistics(instance_id, 'NetworkOut', days=days, unit='Bytes'),
            'network_packets_in': self.get_metric_statistics(instance_id, 'NetworkPacketsIn', days=days),
            'network_packets_out': self.get_metric_statistics(instance_id, 'NetworkPacketsOut', days=days),
            'disk_read_bytes': self.get_metric_statistics(instance_id, 'DiskReadBytes', days=days, unit='Bytes'),
            'disk_write_bytes': self.get_metric_statistics(instance_id, 'DiskWriteBytes', days=days, unit='Bytes'),
            'disk_read_ops': self.get_metric_statistics(instance_id, 'DiskReadOps', days=days),
            'disk_write_ops': self.get_metric_statistics(instance_id, 'DiskWriteOps', days=days),
        }
        
        # Try to get memory metrics from CloudWatch Agent (if available)
        memory_metrics = self.get_metric_statistics(
            instance_id, 
            'mem_used_percent', 
            namespace='CWAgent',
            days=days
        )
        if memory_metrics:
            metrics['memory'] = memory_metrics
        
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
    
    def analyze_metrics(self, metrics: Dict) -> Dict:
        """Analyze collected metrics and return statistics"""
        analysis = {}
        
        # CPU Analysis
        if metrics.get('cpu'):
            analysis['cpu'] = self.calculate_statistics(metrics['cpu'])
        
        # Network Analysis (convert bytes to MB)
        if metrics.get('network_in'):
            stats = self.calculate_statistics(metrics['network_in'])
            analysis['network_in_mb'] = {k: v / (1024 * 1024) for k, v in stats.items()}
        
        if metrics.get('network_out'):
            stats = self.calculate_statistics(metrics['network_out'])
            analysis['network_out_mb'] = {k: v / (1024 * 1024) for k, v in stats.items()}
        
        # Network Packets
        if metrics.get('network_packets_in'):
            analysis['network_packets_in'] = self.calculate_statistics(metrics['network_packets_in'])
        
        if metrics.get('network_packets_out'):
            analysis['network_packets_out'] = self.calculate_statistics(metrics['network_packets_out'])
        
        # Disk I/O (convert bytes to MB)
        if metrics.get('disk_read_bytes'):
            stats = self.calculate_statistics(metrics['disk_read_bytes'])
            analysis['disk_read_mb'] = {k: v / (1024 * 1024) for k, v in stats.items()}
        
        if metrics.get('disk_write_bytes'):
            stats = self.calculate_statistics(metrics['disk_write_bytes'])
            analysis['disk_write_mb'] = {k: v / (1024 * 1024) for k, v in stats.items()}
        
        # Disk Operations
        if metrics.get('disk_read_ops'):
            analysis['disk_read_ops'] = self.calculate_statistics(metrics['disk_read_ops'])
        
        if metrics.get('disk_write_ops'):
            analysis['disk_write_ops'] = self.calculate_statistics(metrics['disk_write_ops'])
        
        # Memory (if available)
        if metrics.get('memory'):
            analysis['memory'] = self.calculate_statistics(metrics['memory'])
        
        return analysis
    
    def get_instance_specs(self, instance_type: str) -> Dict:
        """Get specifications for an instance type"""
        # This is a simplified version. In production, you might want to use
        # AWS Price List API or maintain a comprehensive mapping
        
        # Common instance type specifications (simplified)
        specs_db = {
            # T3 family
            't3.nano': {'vcpu': 2, 'memory_gb': 0.5, 'network_performance': 'Up to 5 Gigabit', 'network_baseline_mbps': 32},
            't3.micro': {'vcpu': 2, 'memory_gb': 1, 'network_performance': 'Up to 5 Gigabit', 'network_baseline_mbps': 64},
            't3.small': {'vcpu': 2, 'memory_gb': 2, 'network_performance': 'Up to 5 Gigabit', 'network_baseline_mbps': 128},
            't3.medium': {'vcpu': 2, 'memory_gb': 4, 'network_performance': 'Up to 5 Gigabit', 'network_baseline_mbps': 256},
            't3.large': {'vcpu': 2, 'memory_gb': 8, 'network_performance': 'Up to 5 Gigabit', 'network_baseline_mbps': 512},
            't3.xlarge': {'vcpu': 4, 'memory_gb': 16, 'network_performance': 'Up to 5 Gigabit', 'network_baseline_mbps': 512},
            't3.2xlarge': {'vcpu': 8, 'memory_gb': 32, 'network_performance': 'Up to 5 Gigabit', 'network_baseline_mbps': 512},
            
            # M5 family
            'm5.large': {'vcpu': 2, 'memory_gb': 8, 'network_performance': 'Up to 10 Gigabit', 'network_baseline_mbps': 750},
            'm5.xlarge': {'vcpu': 4, 'memory_gb': 16, 'network_performance': 'Up to 10 Gigabit', 'network_baseline_mbps': 1250},
            'm5.2xlarge': {'vcpu': 8, 'memory_gb': 32, 'network_performance': 'Up to 10 Gigabit', 'network_baseline_mbps': 2500},
            'm5.4xlarge': {'vcpu': 16, 'memory_gb': 64, 'network_performance': '10 Gigabit', 'network_baseline_mbps': 5000},
            
            # C5 family
            'c5.large': {'vcpu': 2, 'memory_gb': 4, 'network_performance': 'Up to 10 Gigabit', 'network_baseline_mbps': 750},
            'c5.xlarge': {'vcpu': 4, 'memory_gb': 8, 'network_performance': 'Up to 10 Gigabit', 'network_baseline_mbps': 1250},
            'c5.2xlarge': {'vcpu': 8, 'memory_gb': 16, 'network_performance': 'Up to 10 Gigabit', 'network_baseline_mbps': 2500},
            
            # R5 family
            'r5.large': {'vcpu': 2, 'memory_gb': 16, 'network_performance': 'Up to 10 Gigabit', 'network_baseline_mbps': 750},
            'r5.xlarge': {'vcpu': 4, 'memory_gb': 32, 'network_performance': 'Up to 10 Gigabit', 'network_baseline_mbps': 1250},
            'r5.2xlarge': {'vcpu': 8, 'memory_gb': 64, 'network_performance': 'Up to 10 Gigabit', 'network_baseline_mbps': 2500},
        }
        
        return specs_db.get(instance_type, {
            'vcpu': 0,
            'memory_gb': 0,
            'network_performance': 'Unknown',
            'network_baseline_mbps': 0
        })
    
    def recommend_instance_type(
        self, 
        current_type: str, 
        analysis: Dict,
        instance_id: str
    ) -> Dict:
        """Recommend optimal instance type based on analysis"""
        current_specs = self.get_instance_specs(current_type)
        cpu_stats = analysis.get('cpu', {})
        
        recommendation = {
            'current_type': current_type,
            'recommended_type': current_type,
            'action': 'Keep current',
            'reason': [],
            'risk_level': 'Low',
            'estimated_savings_percent': 0,
            'confidence': 'High'
        }
        
        # Check for over-provisioning
        cpu_avg = cpu_stats.get('average', 0)
        cpu_p95 = cpu_stats.get('p95', 0)
        cpu_max = cpu_stats.get('maximum', 0)
        
        thresholds = self.config['thresholds']
        
        # Over-provisioned detection
        if cpu_avg < thresholds['cpu_low'] and cpu_p95 < 40:
            recommendation['action'] = 'Downsize'
            recommendation['reason'].append(f"CPU utilization is low (avg: {cpu_avg:.1f}%, P95: {cpu_p95:.1f}%)")
            
            # Suggest smaller instance
            if current_type.endswith('.2xlarge'):
                recommendation['recommended_type'] = current_type.replace('.2xlarge', '.xlarge')
                recommendation['estimated_savings_percent'] = 50
            elif current_type.endswith('.xlarge'):
                recommendation['recommended_type'] = current_type.replace('.xlarge', '.large')
                recommendation['estimated_savings_percent'] = 50
            elif current_type.endswith('.large'):
                recommendation['recommended_type'] = current_type.replace('.large', '.medium')
                recommendation['estimated_savings_percent'] = 50
            elif current_type.endswith('.medium'):
                recommendation['recommended_type'] = current_type.replace('.medium', '.small')
                recommendation['estimated_savings_percent'] = 50
        
        # Under-provisioned detection
        elif cpu_p95 > thresholds['cpu_high'] or cpu_max > 90:
            recommendation['action'] = 'Upsize'
            recommendation['reason'].append(f"CPU utilization is high (P95: {cpu_p95:.1f}%, Max: {cpu_max:.1f}%)")
            recommendation['risk_level'] = 'High'
            
            # Suggest larger instance
            if current_type.endswith('.small'):
                recommendation['recommended_type'] = current_type.replace('.small', '.medium')
            elif current_type.endswith('.medium'):
                recommendation['recommended_type'] = current_type.replace('.medium', '.large')
            elif current_type.endswith('.large') and not current_type.endswith('.xlarge'):
                recommendation['recommended_type'] = current_type.replace('.large', '.xlarge')
            elif current_type.endswith('.xlarge') and not current_type.endswith('.2xlarge'):
                recommendation['recommended_type'] = current_type.replace('.xlarge', '.2xlarge')
        
        # Check network utilization
        network_in = analysis.get('network_in_mb', {}).get('average', 0)
        network_out = analysis.get('network_out_mb', {}).get('average', 0)
        total_network_mb = network_in + network_out
        
        if total_network_mb > thresholds['network_high_mbps']:
            recommendation['reason'].append(f"High network usage: {total_network_mb:.1f} MB/s average")
            if recommendation['action'] == 'Downsize':
                recommendation['confidence'] = 'Medium'
                recommendation['reason'].append("Consider network requirements before downsizing")
        
        # Check memory if available
        if 'memory' in analysis:
            mem_avg = analysis['memory'].get('average', 0)
            mem_p95 = analysis['memory'].get('p95', 0)
            
            if mem_p95 > thresholds['memory_high']:
                recommendation['reason'].append(f"High memory usage (P95: {mem_p95:.1f}%)")
                if recommendation['action'] == 'Downsize':
                    recommendation['action'] = 'Keep current'
                    recommendation['recommended_type'] = current_type
                    recommendation['reason'].append("Memory usage prevents downsizing")
                    recommendation['risk_level'] = 'Medium'
        
        # If no reason was added, instance is well-sized
        if not recommendation['reason']:
            recommendation['reason'].append("Instance is appropriately sized for current workload")
        
        return recommendation


def display_instances_table(instances: List[Dict]) -> None:
    """Display instances in a formatted table"""
    if not instances:
        click.echo(f"{Fore.YELLOW}No instances found.{Style.RESET_ALL}")
        return
    
    headers = ['#', 'Instance ID', 'Name', 'Type', 'State', 'Private IP']
    rows = []
    
    for idx, inst in enumerate(instances, 1):
        rows.append([
            idx,
            inst['InstanceId'],
            inst['Name'][:30],  # Truncate long names
            inst['InstanceType'],
            inst['State'],
            inst['PrivateIpAddress']
        ])
    
    click.echo(f"\n{Fore.CYAN}Available EC2 Instances:{Style.RESET_ALL}")
    click.echo(tabulate(rows, headers=headers, tablefmt='grid'))


def display_analysis_report(
    instance: Dict, 
    analysis: Dict, 
    recommendation: Dict,
    days: int
) -> None:
    """Display the analysis report in a formatted way"""
    click.echo(f"\n{'=' * 80}")
    click.echo(f"{Fore.GREEN}{Style.BRIGHT}EC2 Instance Analysis Report{Style.RESET_ALL}")
    click.echo(f"{'=' * 80}")
    
    # Instance Information
    click.echo(f"\n{Fore.CYAN}Instance Information:{Style.RESET_ALL}")
    click.echo(f"  Instance ID:    {instance['InstanceId']}")
    click.echo(f"  Name:           {instance['Name']}")
    click.echo(f"  Current Type:   {instance['InstanceType']}")
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
    
    # Network In
    if 'network_in_mb' in analysis:
        net_in = analysis['network_in_mb']
        metrics_data.append([
            'Network In (MB)',
            f"{net_in['average']:.2f}",
            f"{net_in['maximum']:.2f}",
            f"{net_in['p95']:.2f}",
            f"{net_in['p99']:.2f}"
        ])
    
    # Network Out
    if 'network_out_mb' in analysis:
        net_out = analysis['network_out_mb']
        metrics_data.append([
            'Network Out (MB)',
            f"{net_out['average']:.2f}",
            f"{net_out['maximum']:.2f}",
            f"{net_out['p95']:.2f}",
            f"{net_out['p99']:.2f}"
        ])
    
    # Disk Read
    if 'disk_read_mb' in analysis:
        disk_r = analysis['disk_read_mb']
        metrics_data.append([
            'Disk Read (MB)',
            f"{disk_r['average']:.2f}",
            f"{disk_r['maximum']:.2f}",
            f"{disk_r['p95']:.2f}",
            f"{disk_r['p99']:.2f}"
        ])
    
    # Disk Write
    if 'disk_write_mb' in analysis:
        disk_w = analysis['disk_write_mb']
        metrics_data.append([
            'Disk Write (MB)',
            f"{disk_w['average']:.2f}",
            f"{disk_w['maximum']:.2f}",
            f"{disk_w['p95']:.2f}",
            f"{disk_w['p99']:.2f}"
        ])
    
    # Memory
    if 'memory' in analysis:
        mem = analysis['memory']
        metrics_data.append([
            'Memory (%)',
            f"{mem['average']:.2f}",
            f"{mem['maximum']:.2f}",
            f"{mem['p95']:.2f}",
            f"{mem['p99']:.2f}"
        ])
    
    headers = ['Metric', 'Average', 'Maximum', 'P95', 'P99']
    click.echo(tabulate(metrics_data, headers=headers, tablefmt='grid'))
    
    # Recommendation
    click.echo(f"\n{Fore.CYAN}Recommendation:{Style.RESET_ALL}")
    
    action_color = {
        'Keep current': Fore.GREEN,
        'Downsize': Fore.YELLOW,
        'Upsize': Fore.RED
    }.get(recommendation['action'], Fore.WHITE)
    
    risk_color = {
        'Low': Fore.GREEN,
        'Medium': Fore.YELLOW,
        'High': Fore.RED
    }.get(recommendation['risk_level'], Fore.WHITE)
    
    click.echo(f"  Action:         {action_color}{recommendation['action']}{Style.RESET_ALL}")
    click.echo(f"  Recommended:    {Fore.GREEN}{recommendation['recommended_type']}{Style.RESET_ALL}")
    click.echo(f"  Risk Level:     {risk_color}{recommendation['risk_level']}{Style.RESET_ALL}")
    click.echo(f"  Confidence:     {recommendation['confidence']}")
    
    if recommendation['estimated_savings_percent'] > 0:
        click.echo(f"  Est. Savings:   {Fore.GREEN}~{recommendation['estimated_savings_percent']}%{Style.RESET_ALL}")
    
    click.echo(f"\n  Reasons:")
    for reason in recommendation['reason']:
        click.echo(f"    - {reason}")
    
    click.echo(f"\n{'=' * 80}\n")


@click.group()
def cli():
    """EC2 Instance Analyzer - Analyze and optimize your EC2 instances"""
    pass


@cli.command()
@click.option('--region', default=None, help='AWS region (default: from AWS config)')
@click.option('--days', default=7, help='Number of days to analyze (default: 7)')
@click.option('--output-json', type=click.Path(), help='Save results to JSON file')
@click.option('--output-csv', type=click.Path(), help='Save results to CSV file')
@click.option('--instance-id', help='Analyze specific instance ID')
@click.option('--tag', help='Filter by tag (format: Key=Value)')
def analyze(region, days, output_json, output_csv, instance_id, tag):
    """Analyze EC2 instances and get recommendations"""
    
    click.echo(f"{Fore.GREEN}{Style.BRIGHT}EC2 Instance Analyzer{Style.RESET_ALL}")
    click.echo(f"Region: {region or 'default'}\n")
    
    analyzer = EC2Analyzer(region=region)
    
    # Get instances
    filters = None
    if instance_id:
        filters = [{'Name': 'instance-id', 'Values': [instance_id]}]
    elif tag:
        key, value = tag.split('=')
        filters = [
            {'Name': 'instance-state-name', 'Values': ['running']},
            {'Name': f'tag:{key}', 'Values': [value]}
        ]
    
    instances = analyzer.list_instances(filters=filters)
    
    if not instances:
        click.echo(f"{Fore.YELLOW}No running instances found.{Style.RESET_ALL}")
        return
    
    # If instance_id was specified, analyze it directly
    if instance_id:
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
        click.echo(f"\n{Fore.YELLOW}Analyzing {instance['InstanceId']} ({instance['Name']})...{Style.RESET_ALL}")
        
        # Collect metrics
        metrics = analyzer.collect_all_metrics(instance['InstanceId'], days=days)
        
        # Analyze metrics
        analysis = analyzer.analyze_metrics(metrics)
        
        # Get recommendation
        recommendation = analyzer.recommend_instance_type(
            instance['InstanceType'],
            analysis,
            instance['InstanceId']
        )
        
        # Display report
        display_analysis_report(instance, analysis, recommendation, days)
        
        # Store results
        results.append({
            'instance': instance,
            'analysis': analysis,
            'recommendation': recommendation
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
        recommendation = result['recommendation']
        
        output.append({
            'instance_id': instance['InstanceId'],
            'instance_name': instance['Name'],
            'current_type': instance['InstanceType'],
            'analysis': analysis,
            'recommendation': recommendation,
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
            'Instance ID', 'Name', 'Current Type', 
            'CPU Avg (%)', 'CPU P95 (%)', 'CPU Max (%)',
            'Network In Avg (MB)', 'Network Out Avg (MB)',
            'Memory Avg (%)', 'Memory P95 (%)',
            'Action', 'Recommended Type', 'Risk Level', 
            'Est. Savings (%)', 'Reason'
        ])
        
        # Write data
        for result in results:
            instance = result['instance']
            analysis = result['analysis']
            recommendation = result['recommendation']
            
            cpu = analysis.get('cpu', {})
            net_in = analysis.get('network_in_mb', {})
            net_out = analysis.get('network_out_mb', {})
            mem = analysis.get('memory', {})
            
            writer.writerow([
                instance['InstanceId'],
                instance['Name'],
                instance['InstanceType'],
                f"{cpu.get('average', 0):.2f}",
                f"{cpu.get('p95', 0):.2f}",
                f"{cpu.get('maximum', 0):.2f}",
                f"{net_in.get('average', 0):.2f}",
                f"{net_out.get('average', 0):.2f}",
                f"{mem.get('average', 0):.2f}" if mem else 'N/A',
                f"{mem.get('p95', 0):.2f}" if mem else 'N/A',
                recommendation['action'],
                recommendation['recommended_type'],
                recommendation['risk_level'],
                recommendation['estimated_savings_percent'],
                '; '.join(recommendation['reason'])
            ])


@cli.command()
def list_instances_cmd():
    """List all running EC2 instances"""
    analyzer = EC2Analyzer()
    instances = analyzer.list_instances()
    display_instances_table(instances)


if __name__ == '__main__':
    cli()
