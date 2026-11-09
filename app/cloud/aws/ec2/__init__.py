"""EC2 resource discovery."""

from app.cloud.aws.ec2.instances import Ec2Error, Ec2Instance, list_instances, print_instances

__all__ = ["Ec2Error", "Ec2Instance", "list_instances", "print_instances"]