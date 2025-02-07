import time
from typing import List, Dict, Any, Sequence, Union, Optional, Tuple
from dataclasses import dataclass, field
from playwright.async_api import async_playwright, Browser, Page
import asyncio
import importlib.resources
import os
from pathlib import Path


from . import BaseProcessor, BaseTarget, StateDict, documentation, public


def get_script_path(filename: str) -> str:
    """Get the correct path for script files, handling both development and installed scenarios."""
    # Try development path first (src layout)
    dev_path = Path(__file__).parent.parent.parent.parent / "scripts" / "web" / filename
    if dev_path.exists():
        return dev_path.read_text()

    # Fall back to installed package path
    return (
        importlib.resources.files("donew")
        .joinpath(f"scripts/web/{filename}")
        .read_text()
    )

def merge_args(default_args, user_args):
    """Merge default and user provided args, overriding duplicates from default with user provided values.
    Args are expected in the format '--ARG=VALUE'."""
    def parse_arg(arg):
        if arg.startswith("--") and "=" in arg:
            key, val = arg[2:].split("=", 1)
            return key, val
        elif arg.startswith("--"):
            return arg[2:], None
        return None, arg
    merged = {}
    for arg in default_args:
        key, val = parse_arg(arg)
        if key is not None:
            merged[key] = val
    for arg in user_args:
        key, val = parse_arg(arg)
        if key is not None:
            merged[key] = val
    merged_args = []
    for key, val in merged.items():
        if val is not None:
            merged_args.append(f"--{key}={val}")
        else:
            merged_args.append(f"--{key}")
    return merged_args



@dataclass
class ElementMetadata:
    """Rich element metadata incorporating parsing patterns."""

    element_id: int
    element_name: str  # HTML tag name
    element_label: Optional[str]  # HTML label attribute
    element_html: str  # Opening tag HTML
    xpath: str  # Unique XPath
    bounding_box: Optional[Dict[str, float]]
    is_interactive: bool
    element_type: str  # button, link, input, icon, text
    attributes: Dict[str, str]  # All HTML attributes
    computed_styles: Optional[Dict[str, str]]  # Key CSS properties
    listeners: List[str]  # Event listeners
    parent_id: Optional[int]  # Parent element ID
    children_ids: List[int]  # Child element IDs
    state: Dict[str, Any]  # Element state


@dataclass
class Interaction:
    """Record of an interaction with a page element."""

    element_id: int
    interaction_type: str  # e.g., click, type
    timestamp: float
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WebPage(BaseTarget):
    """Manages individual page state and elements."""

    _elements: Dict[int, ElementMetadata] = field(default_factory=dict)
    _interaction_history: List[Interaction] = field(default_factory=list)
    _page: Optional[Page] = None
    _headless: bool = True
    _annotation_enabled: bool = False
    _channel: str = "chromium"

    async def process(self, url: Optional[str] = None) -> "WebPage":
        """Process a webpage and extract its elements.

        **Inputs**
            url: The URL to process.

        **Outputs**
            WebPage: The processed WebPage instance.
        """
        if not self.is_live():
            raise ValueError("Page is not live")
        if not self._page:
            raise ValueError("No page object available")

        if not self._headless and self._channel == "chromium":
            import warnings

            warnings.warn(
                "Running in headed mode may cause navigation errors with HTTP error responses "
                "(like 404, 501) due to a known Chromium bug. "
                "See: https://github.com/microsoft/playwright/issues/33962"
            )

        # Set up navigation handler
        async def handle_navigation():
            if not self._page:
                return
            
            await asyncio.sleep(3)

            # Log navigation as an interaction
            self._interaction_history.append(
                Interaction(
                    element_id=-1,  # No element for navigation
                    interaction_type="navigate",
                    timestamp=time.time(),
                    data={"url": self._page.url},
                )
            )

            # Re-inject and execute element detection script
            script = get_script_path("element_detection.js")
            elements = await self._page.evaluate(script)

            # Update elements
            self._elements = {
                int(id): ElementMetadata(**metadata)
                for id, metadata in elements.items()
            }

            # Re-enable annotations if needed
            if self._annotation_enabled:
                await self.annotation(True)

        # Listen for navigation events
        self._page.on("domcontentloaded", lambda _: asyncio.create_task(handle_navigation()))

        # Set up route handler to continue on error responses
        async def handle_route(route):
            
            response = await route.fetch()
            await route.fulfill(response=response)

        await self._page.route(r"^(?!chrome-extension:).*", handle_route)

        # If no url do nothing for navigation
        if not url:
            return self

        # Initial navigation with error handling
        try:
            await self._page.goto(url, wait_until="load")
            await asyncio.sleep(3)
        except Exception as e:
            if "ERR_HTTP_RESPONSE_CODE_FAILURE" in str(e) and self._headless:
                raise ValueError(
                    "Navigation error with HTTP error responses (like 404, 501) due to a known Chromium bug. "
                    "See: https://github.com/microsoft/playwright/issues/33962"
                    "Try running in headless mode."
                )
            else:
                raise

        # Ensure we process the page after networkidle
        await handle_navigation()

        return self

    def elements(
        self, bbox: Optional[Tuple[float, float, float, float]] = None
    ) -> Dict[int, ElementMetadata]:
        """Get all elements, optionally filtered by bounding box.

        **Inputs**
            bbox (Tuple[float, float, float, float], optional): Bounding box filter (x1, y1, x2, y2).

        **Outputs**
            Dict[int, ElementMetadata]: A dictionary of element IDs to metadata.
        """
        if bbox:
            return {
                id: elem
                for id, elem in self._elements.items()
                if elem.bounding_box
                and all(
                    elem.bounding_box[key] >= bbox[i]
                    for i, key in enumerate(["x", "y", "width", "height"])
                )
            }
        else:
            return self._elements

    def interactions(self) -> List[Interaction]:
        """Get all interactions"""
        return self._interaction_history

    def pw_page(self) -> Page:
        if not self._page:
            raise ValueError("No page object available")
        elif self._page.is_closed():
            raise ValueError("Page is closed")
        else:
            return self._page

    async def click(self, element_id: int):
        """Click an element.

        **Inputs**
            element_id (int): The ID of the element to click.

        **Outputs**
            None
        """
        if not self._page:
            raise ValueError("No live page connection")

        element = self._elements.get(element_id)
        if not element:
            raise ValueError(f"No element found with ID {element_id}")

        await self._page.click(element.xpath)
        self._interaction_history.append(Interaction(element_id, "click", time.time()))

    async def type(self, element_id: int, text: str):
        """Type text into an element.

        **Inputs**
            element_id (int): The ID of the input element.
            text (str): The text to type.

        **Outputs**
            None
        """
        if not self._page:
            raise ValueError("No live page connection")

        element = self._elements.get(element_id)
        if not element:
            raise ValueError(f"No element found with ID {element_id}")

        await self._page.fill(element.xpath, text)
        self._interaction_history.append(
            Interaction(element_id, "type", time.time(), {"text": text})
        )

    def is_live(self) -> bool:
        try:
            self.pw_page()
            return True
        except ValueError:
            return False

    def disconnect(self):
        """Disconnect from the page"""
        if self._page:
            self._page = None

    async def image(
        self,
        element_id: Optional[int] = None,
        bbox: Optional[Tuple[float, float, float, float]] = None,
        viewport: Optional[bool] = None,
    ) -> bytes:
        """Get element's image content in PNG format.

        **Inputs**
            element_id: The ID of the element to get the image from. If None, gets the entire page.
            bbox: A tuple of (x1, y1, x2, y2) to crop the image to.
            viewport: Whether to get the image of the viewport or the entire page. Only applies if element_id is None.

        **Outputs**
            bytes: The image content.
        """
        if element_id:
            element = self._elements.get(element_id)
            if not element:
                raise ValueError(f"No element found with ID {element_id}")
            result = await self.pw_page().locator(element.xpath).screenshot()
        elif element_id is None and bbox is None:
            result = await self.pw_page().screenshot(full_page=viewport)
        elif bbox is not None:
            # Calculate clip dimensions from bbox coordinates
            x1, y1, x2, y2 = bbox
            width = x2 - x1
            height = y2 - y1

            # Use Playwright's clip option for precise region capture
            result = await self.pw_page().screenshot(
                clip={"x": x1, "y": y1, "width": width, "height": height}
            )
        else:
            raise ValueError("Either element_id or bbox must be provided")

        return result

    async def text(
        self,
        element_id: Optional[int] = None,
    ) -> str:
        """Get element's text content with interactive elements marked.
        **Inputs**
            element_id: The ID of the element to get the text from. If None, gets the page content.
        **Outputs**
            str: The text content with interactive elements marked with [id@type#subtype].
        """
        if not self._page:
            raise ValueError("No live page connection")

        # First, temporarily modify interactive elements to show their IDs and types
        script = get_script_path("text_markers.js")
        num_modified = await self._page.evaluate(script)

        try:
            # Get text content
            if element_id:
                element = self._elements.get(
                    element_id
                )  # Now using int directly since type hint enforces it
                if not element:
                    raise ValueError(f"No element found with ID {element_id}")
                result = await self.pw_page().locator(element.xpath).inner_text()
            else:
                result = await self.pw_page().locator("body").inner_text()

            return result

        finally:
            # Restore original state
            restore_script = get_script_path("restore_text_markers.js")
            await self._page.evaluate(restore_script)

    async def scroll(self, element_id: int):
        """Scroll element into view

        **Inputs**
            element_id: The ID of the element to scroll.

        **Outputs**
            None
        """
        if not self._page:
            raise ValueError("No live page connection")

        element = self._elements.get(element_id)
        if not element:
            raise ValueError(f"No element found with ID {element_id}")

        await self._page.evaluate(
            f"document.querySelector(\"[data-dosee-element-id='{element_id}']\").scrollIntoView()"
        )
        self._interaction_history.append(Interaction(element_id, "scroll", time.time()))

    async def cookies(
        self, cookies: Optional[Dict[str, str]] = None
    ) -> Sequence[Dict[str, str]]:
        """Gets or sets cookies for the current browser context using Playwright's native cookie handling.
        Direct passthrough to Browser's context.cookies() and context.add_cookies() methods.

        **Inputs**
            cookies : Dict[str, str], optional
                Cookie dictionary to set using Playwright's add_cookies.
                If None, returns current cookies via Playwright's cookies() method.

        **Outputs**
            Dict[str, str]
                Dictionary of current cookies from Playwright's context.cookies()


        **Usage**
        ```python
        # Get current  cookies
        current_cookies = await processor.cookies()

        # Set Playwright cookies and get updated state
        new_cookies = await processor.cookies({
            "session": "abc123",
            "user_id": "12345"
        })
        ```
        """
        if not self._page:
            raise ValueError("No live page connection")

        if cookies is not None:
            await self._page.context.add_cookies(cookies)  # type: ignore

        return await self._page.context.cookies()  # type: ignore

    async def storage(
        self, storage_state: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Gets or sets storage state (localStorage and sessionStorage) for the current page.

        **Inputs**
            storage_state : Dict[str, Any], optional
                Storage state to set. Should be in format:
                {
                    "localStorage": {"key": "value", ...},
                    "sessionStorage": {"key": "value", ...}
                }
                If None, returns current storage state.

        **Outputs**
            Dict[str, Any]
                Dictionary containing current localStorage and sessionStorage state:
                {
                    "localStorage": {...},
                    "sessionStorage": {...}
                }

        **Raises**
            ValueError
                If no live page connection exists

        **Usage**
        ```python
        # Get current storage state
        state = await page.storage()

        # Set new storage state
        await page.storage({
            "localStorage": {"user": "jane"},
            "sessionStorage": {"token": "xyz789"}
        })
        ```
        """
        if not self._page:
            raise ValueError("No live page connection")

        if storage_state is not None:
            # Set localStorage
            if "localStorage" in storage_state:
                for key, value in storage_state["localStorage"].items():
                    await self._page.evaluate(
                        f"localStorage.setItem('{key}', '{value}')"
                    )

            # Set sessionStorage
            if "sessionStorage" in storage_state:
                for key, value in storage_state["sessionStorage"].items():
                    await self._page.evaluate(
                        f"sessionStorage.setItem('{key}', '{value}')"
                    )

        # Get current storage state
        return await self._page.evaluate(
            """() => {
            return {
                localStorage: Object.fromEntries(Object.entries(localStorage)),
                sessionStorage: Object.fromEntries(Object.entries(sessionStorage))
            };
        }"""
        )

    async def annotation(self, enabled: bool = True) -> None:
        """Toggle visual annotation of elements on the page.

        **Inputs**
            enabled: Whether to enable or disable annotation

        **Outputs**
            None
        """
        self._annotation_enabled = enabled
        if self._page:
            if enabled:
                await self._inject_annotation_styles()
                await self._highlight_elements()
            else:
                await self._remove_annotations()

    async def _inject_annotation_styles(self) -> None:
        """Inject CSS styles for element annotation"""
        styles = get_script_path("highlight_styles.css")
        await self._page.add_style_tag(content=styles)  # type: ignore

    async def _highlight_elements(self) -> None:
        """Add highlight overlays to all detected elements"""
        script = get_script_path("highlight_elements.js")
        await self._page.evaluate(script)  # type: ignore

    async def _remove_annotations(self) -> None:
        """Remove all element annotations from the page"""
        await self._page.evaluate(  # type: ignore
            "document.querySelectorAll('.DoSee-highlight').forEach(el => el.remove())"
        )

    async def close(self):
        """Close the page"""
        if self._page:
            await self._page.close()
            self._page = None

    def interaction_history(self) -> List[Tuple[float, str, Dict[str, Any]]]:
        """Get page's interaction history as [(timestamp, action_type, metadata)]
        Metadata includes element info, data, and page URL

        **Outputs**
            List[Tuple[float, str, Dict[str, Any]]]: The interaction history.
        """
        history = []

        # Process all interactions including navigation
        for interaction in self._interaction_history:
            if interaction.interaction_type == "navigate":
                metadata = {"url": interaction.data["url"]}
            else:
                element = self._elements.get(interaction.element_id)
                metadata = {
                    "element_type": element.element_type if element else None,
                    "element_label": element.element_label if element else None,
                    "xpath": element.xpath if element else None,
                    "data": interaction.data,
                }

            history.append(
                (interaction.timestamp, interaction.interaction_type, metadata)
            )

        return history

    async def evaluate(self, expression: str) -> Any:
        """Evaluate JavaScript in the current page.
        Returns the value of the `expression` invocation.

        If the function passed to the `browser.evaluate()` returns a [Promise], then `browser.evaluate()` would
        wait for the promise to resolve and return its value.

        If the function passed to the `browser.evaluate()` returns a non-[Serializable] value, then
        `browser.evaluate()` resolves to `undefined`. Browser also supports transferring some additional values
        that are not serializable by `JSON`: `-0`, `NaN`, `Infinity`, `-Infinity`.

        **Inputs**
            expression: The JavaScript expression to evaluate.

        **Outputs**
            Any: The value of the expression.



        """

        return await self._page.evaluate(script)  # type: ignore


@dataclass
class WebBrowser(BaseTarget):
    """Manages browser session and page history."""

    _browser: Optional[Browser] = None
    _pages: List[WebPage] = field(default_factory=list)
    _headless: bool = True
    _channel: str = "chromium"
    _interaction_history: List[Tuple[int, int]] = field(
        default_factory=list
    )  # (page_index, interaction_index)

    def _current_page(self) -> WebPage:
        """Internal method to get current page."""
        if not self._pages:
            raise ValueError(
                "No pages available Initiate a browser and add a page first"
            )
        return self._pages[-1]

    @public(order=1)
    def goto(self, url: str):
        """Goto to a URL in a new page.

        Args:
            url (str): The URL to navigate to.
        """
        return self._sync(self.a_goto(url))

    async def a_goto(self, url: str):
        """Goto to a URL in a new page.

        Args:
            url (str): The URL to navigate to.
        """
        if not self._browser:
            raise ValueError("No browser session")
        current_page = self._current_page()
        pw_page = current_page.pw_page()
        current_page._page = None
        new_page = WebPage(
            _page=pw_page,
            _annotation_enabled=current_page._annotation_enabled,
            _headless=self._headless,
            _channel=self._channel,
        )
        await new_page.process(url)
        self._pages.append(new_page)

    @public(order=2)
    @documentation(extends=WebPage.annotation)
    def annotation(self, enabled: bool = True) -> None:
        """Control visual annotation of elements on the current page.

        Args:
            enabled: Whether to enable or disable annotation
        """
        return self._sync(self.a_annotation(enabled))

    async def a_annotation(self, enabled: bool = True) -> None:
        """Control visual annotation of elements on the current page.

        Args:
            enabled: Whether to enable or disable annotation
        """
        if self._pages:
            await self._current_page().annotation(enabled)

    @public(order=3)
    @documentation(extends=WebPage.cookies)
    def cookies(
        self, cookies: Optional[Dict[str, str]] = None
    ) -> Sequence[Dict[str, str]]:
        return self._sync(self.a_cookies(cookies))

    async def a_cookies(
        self, cookies: Optional[Dict[str, str]] = None
    ) -> Sequence[Dict[str, str]]:
        return await self._current_page().cookies(cookies)

    @public(order=4)
    @documentation(extends=WebPage.storage)
    def storage(
        self, storage_state: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Dict[str, str]]:
        return self._sync(self.a_storage(storage_state))

    async def a_storage(
        self, storage_state: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Dict[str, str]]:
        return await self._current_page().storage(storage_state)

    @public(order=5)
    @documentation(extends=WebPage.click)
    def click(self, element_id: int):
        return self._sync(self.a_click(element_id))

    async def a_click(self, element_id: int):
        return await self._current_page().click(element_id)

    @public(order=6)
    @documentation(extends=WebPage.type)
    def type(self, element_id: int, text: str):
        return self._sync(self.a_type(element_id, text))

    async def a_type(self, element_id: int, text: str):
        return await self._current_page().type(element_id, text)

    @public(order=7)
    @documentation(extends=WebPage.image)
    def image(
        self,
        element_id: Optional[int] = None,
        bbox: Optional[Tuple[float, float, float, float]] = None,
        viewport: Optional[bool] = None,
    ) -> bytes:
        """Get image content from an element."""
        return self._sync(self.a_image(element_id, bbox, viewport))

    async def a_image(
        self,
        element_id: Optional[int] = None,
        bbox: Optional[Tuple[float, float, float, float]] = None,
        viewport: Optional[bool] = None,
    ) -> bytes:
        """Async version of image."""
        return await self._current_page().a_image(element_id, bbox, viewport)

    @public(order=8)
    @documentation(extends=WebPage.text)
    def text(self, element_id: Optional[int] = None) -> str:
        return self._sync(self.a_text(element_id))

    async def a_text(self, element_id: Optional[int] = None) -> str:
        """Async version of text."""
        return await self._current_page().text(element_id)

    def text(self, element_id: Optional[int] = None) -> str:
        """Get text content from an element."""
        return self._sync(self.a_text(element_id))

    @public(order=9)
    @documentation(extends=WebPage.elements)
    def elements(
        self, bbox: Optional[Tuple[float, float, float, float]] = None
    ) -> Dict[int, ElementMetadata]:
        """Get all elements on the current page."""
        return self._current_page().elements(bbox)

    async def _get_state_dict(self) -> StateDict:
        """Get browser state including page history and interactions."""
        current_page = self._current_page() if self._pages else None

        # Get element type counts if we have a current page
        element_counts = {"buttons": 0, "inputs": 0, "links": 0, "text": 0, "images": 0}
        if current_page:
            for elem in current_page._elements.values():
                if elem.element_type == "button":
                    element_counts["buttons"] += 1
                elif elem.element_type == "input":
                    element_counts["inputs"] += 1
                elif elem.element_type == "link":
                    element_counts["links"] += 1
                elif elem.element_type == "icon":
                    element_counts["images"] += 1
                else:
                    element_counts["text"] += 1

        # Build timeline from all pages' histories
        timeline_rows = []
        for page in self._pages:
            for timestamp, action_type, metadata in page.interaction_history():
                time_str = time.strftime("%H:%M:%S", time.localtime(timestamp))

                # Format action based on type
                if action_type == "navigate":
                    action = f"Navigated to {metadata['url']}"
                elif action_type == "type":
                    text = metadata["data"].get("text", "")
                    xpath = metadata.get("xpath", "unknown")
                    label = metadata.get("element_label", "")
                    element_desc = f'"{label}" ({xpath})' if label else xpath
                    action = f'Typed value: "{text}" to {element_desc}'
                elif action_type == "click":
                    xpath = metadata.get("xpath", "unknown")
                    label = metadata.get("element_label", "")
                    element_desc = f'"{label}" ({xpath})' if label else xpath
                    action = f"Clicked {element_desc}"
                else:
                    xpath = metadata.get("xpath", "unknown")
                    label = metadata.get("element_label", "")
                    element_desc = f'"{label}" ({xpath})' if label else xpath
                    action = f"Interacted with {element_desc}"

                timeline_rows.append([time_str, action])

        return {
            "sections": [
                {
                    "name": "Timeline",
                    "type": "table",
                    "headers": ["Time", "Action"],
                    "rows": timeline_rows,
                },
                {
                    "name": "Current State",
                    "type": "keyvalue",
                    "data": {
                        "Page": {
                            "URL": (
                                current_page.pw_page().url
                                if current_page and current_page.is_live()
                                else "disconnected"
                            ),
                            "Title": (
                                await current_page.pw_page().title()
                                if current_page and current_page.is_live()
                                else "N/A"
                            ),
                            "Element Count": (
                                str(len(current_page._elements))
                                if current_page
                                else "0"
                            ),
                        },
                        "Elements": {
                            "Interactive": f"{element_counts['buttons']} buttons, {element_counts['inputs']} inputs, {element_counts['links']} links",
                            "Text Elements": f"{element_counts['text']}",
                            "Images": f"{element_counts['images']}",
                        },
                        "Browser": {
                            "Active": str(bool(self._browser)),
                            "Pages in History": str(len(self._pages)),
                            "Cookies": f"{len(await current_page.cookies()) if current_page and current_page.is_live() else 0} active",
                        },
                    },
                },
            ]
        }

    @public(order=10)
    @documentation(
        template="{extendee}",
        extends=WebPage.evaluate,
    )
    def evaluate(self, script: str) -> Any:
        """Evaluate JavaScript in the current page."""
        return self._sync(self.a_evaluate(script))

    async def a_evaluate(self, script: str) -> Any:
        """Async version of evaluate."""
        return await self._current_page().evaluate(script)

    @public(order=11)
    @documentation(extends=WebPage.close)
    def close(self):
        """Close the browser and clean up resources."""
        return self._sync(self.a_close())

    async def a_close(self):
        """Async version of close."""
        if self._browser:
            await self._browser.close()
            self._browser = None
            self._pages.clear()


class WebProcessor(BaseProcessor[Union[str, Page]]):
    """Main processor for web page analysis and interaction."""

    def __init__(self, **kwargs):
        #  a helper function to merge default args with user provided args
       
        default_args = ["--disable-infobars","--remote-debugging-pipe","--no-startup-window"]
        user_args = kwargs.get("args", [])
        if not isinstance(user_args, list):
            user_args = [user_args]
        merged_args = merge_args(default_args, user_args) if user_args else default_args

        self._kwargs = {
            "headless": kwargs.get("headless", True),
            "executable_path": kwargs.get("chrome_path", None),
            "user_data_dir": kwargs.get("user_data_dir", None),
            "channel": kwargs.get("channel", "chromium"),
            "no_viewport": kwargs.get("no_viewport", True),
            "screen": kwargs.get("screen", {"width": 1920, "height": 1080}),
            "bypass_csp": kwargs.get("bypass_csp", False),
            "args": merged_args
        }
        

    def documentation(self) -> List[str]:
        """Documentation for DO.Browse"""
        docs = WebBrowser().documentation()
        return docs
    
    async def _launch_browser(self):
        playwright = await async_playwright().start()
        if self._kwargs["user_data_dir"]:
            self._kwargs["ignore_default_args"] = True
          
            browser = await playwright.chromium.launch_persistent_context(**self._kwargs)
        else:
            kwargs = {k: v for k, v in self._kwargs.items() if k in ["headless", "executable_path", "channel"]}

            browser = await playwright.chromium.launch(
                **kwargs
            )
        return browser

    async def a_process(self) -> WebBrowser:
        """Async version of process.
        Returns:
            WebBrowser: A WebBrowser instance.
        """
        # Initialize Playwright and browser
        browser = await self._launch_browser()
        pw_page = await browser.new_page()
        web_page = WebPage(
            _page=pw_page, _headless=self._kwargs["headless"], _channel=self._kwargs["channel"],  _annotation_enabled=False
        )
        await web_page.process()

        web_browser = WebBrowser(
            _browser=browser, _pages=[web_page], _headless=self._kwargs["headless"]
        )

        return web_browser

    def process(self) -> WebBrowser:
        """Sync version of process.

        Args:
            source: The URL to process or an existing Page object.

        Returns:
            List[BaseTarget]: A list containing the WebBrowser instance.
        """
        return self._sync(self.a_process())
