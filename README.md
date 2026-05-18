# gdex-web-services

A lightweight FastAPI service for running ad-hoc data processing and convenience jobs in support of the [GDEX](https://gdex.ucar.edu) data portal. Rather than burdening the main web portal with one-off or background tasks, this service provides a simple, independently deployable API for those operations.

## Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run the development server
uvicorn app.main:app --reload --port 8080
```

API docs are available at `http://localhost:8080/docs` once the server is running.

## Deployment

Pushing to `main` triggers the GitHub Actions workflow, which builds a new Docker image and pushes it to Harbor (`hub.k8s.ucar.edu/riley_gdex_test/gdex-web-services`). The app is deployed to Kubernetes via the Helm chart in `app-chart/`.

```bash
# Deploy with Helm
helm upgrade --install gdex-webservices ./app-chart -n <namespace>

# Deploy a test instance
helm upgrade --install gdex-webservices ./app-chart -n <namespace> --set testName=<your-name>
```
