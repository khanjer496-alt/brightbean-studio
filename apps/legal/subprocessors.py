"""Third parties that process customer data on PostDelegate's behalf."""

from django.conf import settings


def subprocessors():
    return [
        {
            "name": "Hetzner Online GmbH",
            "purpose": "Servers and database hosting for the application.",
            "location": "Germany or Finland (EU), depending on the deployment region",
            "data": "All service data",
        },
        {
            "name": "Cloudflare, Inc.",
            "purpose": "DNS, CDN and DDoS protection in front of the service, and R2 object storage for uploaded media.",
            "location": "Global network; R2 storage jurisdiction as configured for the bucket",
            "data": "Request metadata (IP address), uploaded media files",
        },
        {
            "name": "Stripe, Inc.",
            "purpose": "Subscription billing and payment processing. We never see full card numbers.",
            "location": "United States and other regions where Stripe operates",
            "data": "Billing contact email, organization identifier, payment and invoice data",
        },
        {
            "name": "Anthropic, PBC",
            "purpose": "AI model provider for the AI writing assistant and AI reply drafts, only when a user uses those features.",
            "location": "United States",
            "data": "Text the user submits to the assistant, and related context such as the message being replied to",
        },
        {
            "name": settings.LEGAL_EMAIL_PROVIDER,
            "purpose": "Delivery of transactional email (sign-in, verification, notifications).",
            "location": "United States (confirm with the provider)",
            "data": "Recipient email address, message content",
        },
    ]
