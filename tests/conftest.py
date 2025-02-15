import os
import sys
import pytest
from typing import AsyncGenerator
import asyncio
import aiohttp

# Add src directory to Python path
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
)



def pytest_addoption(parser):
    parser.addoption(
        "--httpbin-url",
        default="http://localhost:8070",
        help="URL for httpbin (default: http://localhost:8070)",
    )



@pytest.fixture
def httpbin_url(request):
    """Get the httpbin URL from command line or environment variable."""
    return os.getenv("HTTPBIN_URL", request.config.getoption("--httpbin-url"))


@pytest.fixture
async def httpbin_available(httpbin_url) -> AsyncGenerator[bool, None]:
    """Check if httpbin is available before running tests."""
    max_retries = 5
    retry_delay = 1

    async def check_httpbin():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{httpbin_url}/get") as response:
                    return response.status == 200
        except:
            return False

    for i in range(max_retries):
        if await check_httpbin():
            break
        if i < max_retries - 1:
            await asyncio.sleep(retry_delay)
    else:
        pytest.skip(f"httpbin not available at {httpbin_url}")

    yield True
