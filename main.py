import asyncio
import json
import logging
from urllib.parse import urljoin
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


BASE_TIMEOUT = 3000
RETRY_WAIT = 2


def retry_with_logging(func):
    async def retry_connection(*args, **kwargs):
        min_attempt_cnt = 1
        max_attempts_cnt = 3
        while min_attempt_cnt <= max_attempts_cnt:
            try:
                result = await func(*args, **kwargs)
                if min_attempt_cnt > 1:
                    logger.info(f"Success on attempt {min_attempt_cnt} for {func.__name__}")
                return result
            except Exception as e:
                if min_attempt_cnt == 1:
                    logger.error(f"Attempt 1 failed for {func.__name__}: {str(e)}")
                else:
                    logger.error(f"Attempt {min_attempt_cnt} failed for {func.__name__}: {str(e)}")
                if min_attempt_cnt < max_attempts_cnt:
                    logger.info(f"Retrying in {RETRY_WAIT} seconds... ({min_attempt_cnt}/{max_attempts_cnt})")
                    await asyncio.sleep(RETRY_WAIT)
                    min_attempt_cnt += 1
                else:
                    logger.error(f"All {max_attempts_cnt} attempts exhausted for {func.__name__}, raising exception")
                    raise
        return None

    return retry_connection


class LaptopsScraper:
    def __init__(self, url):
        self.url = url
        self.collected_laptops_data = []

    @retry_with_logging
    async def make_requests(self, browser):
        page = await browser.new_page()
        try:
            logger.info(f"Navigating to {self.url}")
            await page.goto(self.url, timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=10000)
            await self.extract_data_from_page(page)
            await self.get_pagination(page)
        finally:
            await page.close()

    @retry_with_logging
    async def extract_data_from_page(self, page):
        try:
            await page.wait_for_selector('div[class="card thumbnail"]', timeout=10000)
            laptops_items = await page.locator('div[class="card thumbnail"]').all()
            logger.info(f"Found {len(laptops_items)} products on current page")
            for laptops_data in laptops_items:
                try:
                    laptops_title = await laptops_data.locator('a[class="title"]').get_attribute('title')
                    laptops_price = await laptops_data.locator('div.caption [itemprop="price"]').inner_text()
                    href = await laptops_data.locator('a.title').get_attribute('href')
                    product_url = urljoin(page.url, href) if href else None
                    try:
                        laptops_rating_attr = await laptops_data.locator('div[class="ratings"] p[data-rating]').get_attribute('data-rating')
                        laptops_rating = int(laptops_rating_attr) if laptops_rating_attr else 0
                    except (ValueError, TypeError):
                        laptops_rating = 0
                    try:
                        laptops_reviews_text = await laptops_data.locator('[itemprop="reviewCount"]').inner_text()
                        laptops_reviews_count = int(laptops_reviews_text.strip().split()[0]) if laptops_reviews_text else 0
                    except (ValueError, IndexError, AttributeError):
                        laptops_reviews_count = 0
                    if laptops_title and laptops_price and product_url:
                        self.collected_laptops_data.append({
                            "title": laptops_title.strip(),
                            "price": laptops_price.strip(),
                            "rating": laptops_rating,
                            "reviews_count": laptops_reviews_count,
                            "product_url": product_url
                        })
                    else:
                        logger.warning("Missing essential data for product, skipping")
                except Exception as e:
                    logger.warning(f"Error parsing individual item: {e}")
                    continue

        except Exception as e:
            logger.error(f"Error scraping current page: {e}")
            raise

    @retry_with_logging
    async def get_pagination(self, page):
        page_count = 1
        while True:
            try:
                await page.wait_for_selector('ul.pagination', timeout=5000)
                next_button = page.locator('ul.pagination li.next a.page-link')
                if await next_button.count() == 0:
                    logger.info("No more pages found.")
                    break
                # Check if next button is disabled
                next_li = page.locator('ul.pagination li.next')
                is_disabled = await next_li.get_attribute('class')
                if is_disabled and 'disabled' in is_disabled:
                    logger.info("Next button is disabled, no more pages.")
                    break
                await next_button.click()
                await page.wait_for_load_state("networkidle", timeout=15000)
                page_count += 1
                logger.info(f"Scraping page {page_count}")
                await self.extract_data_from_page(page)
            except PlaywrightTimeoutError:
                logger.warning(f"Timeout on page {page_count}, breaking pagination...")
                break
            except Exception as e:
                logger.warning(f"Pagination failed on page {page_count}: {e}")
                break

        logger.info(f"Scraped {len(self.collected_laptops_data)} products from {page_count} pages.")

    @retry_with_logging
    async def get_product_description(self, browser, product):
        detail_page = await browser.new_page()
        try:
            logger.info(f"Visiting product page: {product['product_url']}")
            await detail_page.goto(product["product_url"], timeout=15000)
            await detail_page.wait_for_load_state("networkidle", timeout=10000)
            try:
                await detail_page.wait_for_selector('[itemprop="description"]', timeout=5000)
                description_elem = detail_page.locator('[itemprop="description"]').first
                description = await description_elem.inner_text()
                product["description"] = description.strip() if description else "N/A"
            except PlaywrightTimeoutError:
                logger.warning(f"Description element not found for: {product['product_url']}")
                product["description"] = "N/A"
            return product

        except Exception as e:
            logger.error(f"Error getting product description: {e}")
            product["description"] = "N/A"
            return product
        finally:
            await detail_page.close()

    async def get_each_product_page_url(self, browser, items_cnt=0):
        final_data = []
        products_to_process = self.collected_laptops_data[items_cnt:2]
        for idx, product in enumerate(products_to_process, start=items_cnt):
            try:
                result = await self.get_product_description(browser, product.copy())
                final_data.append(result)
                logger.info(f"Processed product {idx + 1}/{len(products_to_process)}")
            except Exception as e:
                logger.warning(f"Failed to get description for {product.get('product_url', 'Unknown URL')}: {e}")
                product_copy = product.copy()
                product_copy["description"] = "N/A"
                final_data.append(product_copy)
        logger.info(f"Collected {len(final_data)} products with descriptions.")
        return final_data


async def main():
    # url = "https://webscraper.io/test-sites/e-commerce/static/computers/laptops"
    url = "https://webscraper.io/test-sites/e-commerce/allinone/computers/laptops"

    laptops_scraper = LaptopsScraper(url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            await laptops_scraper.make_requests(browser)
            all_results = await laptops_scraper.get_each_product_page_url(browser)
            output_file = "e-commerce-laptops.json"
            try:
                with open(output_file, mode="a", encoding="utf-8") as f:
                    json.dump(all_results, f, indent=2, ensure_ascii=False)
                logger.info(f"Successfully saved {len(all_results)} products to {output_file}")
            except IOError as E:
                logger.error(f"Failed to save results to file: {E}")
        except Exception as E:
            logger.error(f"Scraping failed: {E}")
        finally:
            await browser.close()
            logger.info("Browser closed.")


if __name__ == "__main__":
    asyncio.run(main())