#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later

"""linkoping-parking-finder."""

from __future__ import annotations

import contextlib
from enum import StrEnum
import functools
import json
import logging
import os
import pathlib
import re
import signal
import sys
import time

# Setup browser context
from typing import TYPE_CHECKING, Callable, ClassVar, Never

from playwright.sync_api import (
    Browser,
    ElementHandle,
    Error as PlaywrightError,
    Page,
    sync_playwright,
)
from pydantic import BaseModel, ConfigDict, StrictStr, TypeAdapter, ValidationError
from tabulate import tabulate

# pyright: reportMissingTypeStubs=false
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client
from yaspin import yaspin

from parking_finder.parking_logger import setup_logging

if TYPE_CHECKING:
    from types import FrameType

    from playwright.sync_api import Page
    from twilio.rest.api.v2010.account.message import MessageInstance, MessageList
    from yaspin.core import Yaspin


# Labels for the parking spot information.
class ParkingLabel(StrEnum):
    """Labels representing a parking spot."""

    access = "Tillträde:"
    address = "Adress:"
    area = "Område:"
    interest = "Antal intresse:"
    rent = "Hyra:"
    kind = "Type:"


# Attributes for the parking spot information.
# We use this model to represent a parkin spot.
class Parking(BaseModel):
    """Attributes representing a parking spot.

    Attributes:
        access (str): The date of access.
        address (str): The address of the parking spot.
        area (str): The area where the parking spot is located.
        interest (str): The number of interested parties.
        rent (str): The rental price.
        kind (str): The type of parking spot.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    access: StrictStr
    address: StrictStr
    area: StrictStr
    interest: StrictStr
    rent: StrictStr
    kind: StrictStr


CACHE_DIR = pathlib.Path.home() / ".cache" / "parking_finder"
STATE_FILE_NAME = ".parking_finder-cache.json"
STATE_FILE_PATH = CACHE_DIR / STATE_FILE_NAME

# Area codes and their display names
AREA_MAP = {
    "ABYGOT": "Gottfridsberg / Åbylund",
    "INNER": "Innerstaden",
    "JOHA": "Johannelund",
    "LAMBO": "Lambohov / Vallastaden",
    "MAJBER": "Berga / Majelden",
    "RYD": "Ryd",
    "SKATTE": "Skattegården",
    "55TRY": "Senior / Senior+",
    "T1VAFR": "Ebbepark / T1 / Valla",
    "VASA": "Vasastaden",
    "VIDULL": "Vidingsjö / Ullstämma",
    "YTTER": "Ytterområde",
}

EMOJI_MAP: dict[str, str] = {
    "parkering husvagn": "🚗 🏕️ 🅿️",
    "parkeringsdäck": "🚗 🅿️",
    "parkering laddplats": "🚗 ⚡ 🅿️",
    "parkering motorvärmare": "🚗 🔌 🅿️",
    "parkeringsplats": "🚗 🅿️",
    "bilpl på mark h-cap": "🚗 🅿️",
    "carport motorvärmare": "🚗 🔌 🏠",
    "carport": "🚗 🏠",
    "varmgarage": "🚗 🏢 🔥",
    "kallgarage": "🚗 🏢",
    "centralgarage dubbelplats": "🚗 🚗 🏢",
    "centralgarage laddstolpe": "🚗 ⚡ 🏢",
    "centralgarage uppvärmt": "🚗 🏢 🔥",
    "centralgarage": "🚗 🏢 ❄️",
    "varmgarage ej enskilt": "🚗 🚧 🏢 🔥",
    "kallgarage ej enskilt": "🚗 🚧 🏢 ❄️",
    "garage dubbelplats": "🚗🚗 🏠",
    "garage ej enskilt, egen port": "🚗 🏢",
    "garage motorvärmare ej enskilt": "🚗 ⚡ 🏢",
    "garage ej enskilt": "🚗 🏢",
    "centralgarage inhägnad plats": "🚗 🔒 🏢",
    "varmgarage för motorcykel": "🏍️ 🔥",
    "kallgarage för motorcykel": "🏍️ ❄️",
    "garage motorcykel utan nät/vägg": "🏍️ 🚧",
}


# Base URL for the website
BASE_URL = "https://www.stangastaden.se/sokledigt/bilplats/"
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
WHATSAPP_FROM = os.getenv("WHATSAPP_FROM", "")
WHATSAPP_TO = os.getenv("WHATSAPP_TO", "")
SPINNER_TEXT = "Hang tight while we fetch parking spaces from Stångåstaden"

# Create global logger instance
logger = logging.getLogger(__name__)


def validate_and_display_areas(input_args: list[str]) -> list[str]:
    """Parse, validate, and display the given area codes.

    Supports comma-separated inputs from command-line arguments.
    If no valid areas are provided after parsing, returns an empty list
    to search all areas. Exits if any invalid area code is found.

    Args:
        input_args: List of area codes provided as command-line arguments.

    Returns:
        List of valid area codes.

    """
    failed_codes: list[str] = []
    parsed_codes: list[str] = []

    # Process each command-line argument, splitting by comma.
    if input_args:
        logger.info("given area codes: %s", input_args)
    else:
        logger.info("no area codes provided")

    for arg_string in input_args:
        for code_part in arg_string.split(","):
            stripped_code = code_part.strip()
            if stripped_code:
                parsed_codes.append(stripped_code)
    logger.debug("parsed area codes into segments: %s", parsed_codes)

    # Validate area codes, if any are invalid, log and exit.
    if parsed_codes:
        # Validate given area codes, if any are invalid, log and exit.
        failed_codes = [code for code in parsed_codes if code not in AREA_MAP]
        if failed_codes:
            print(f"invalid area code '{', '.join(failed_codes)}'")
            print_valid_codes(1)

    return parsed_codes


def print_valid_codes(exit_code: int) -> Never:
    """
    Print a list of valid area codes and exits with the given exit code.

    Args:
        exit_code (int): The exit code to use when exiting.
    """
    print("valid area codes:")
    for area_short, area in AREA_MAP.items():
        print(f" » {area} ({area_short})")

    exit_with_status(exit_code)


def exit_with_status(exit_code: int) -> Never:
    """
    Exit the program with the given exit code.

    Args:
        exit_code (int): The exit code to use when exiting.
    """
    logger.debug("exit with status %s", exit_code)
    sys.exit(exit_code)


def build_url(areas_parsed: list[str]) -> str:
    """
    Build a url for the given area codes.

    Args:
        areas_parsed (list[str]): The area codes to use.

    Returns:
        str: The parameters for the url.
    """
    params = "".join(f"&omraden%5B%5D={c}" for c in areas_parsed)
    logger.debug("constructed params '%s'", params)

    return f"{BASE_URL}?actionId={params}"


def log_playwright_error_and_exit(
    e: Exception,
    msg: str,
    browser: Browser,
) -> Never:
    """Logs a Playwright error and exits the program.

    Args:
        e (Exception): The exception to log.
        msg (str): The prefix to use for the error message.
        browser(Browser): The browser instance to close.
    """
    full_error_message = str(e).strip()

    # Determine the message to log based on debug level.
    # If not in debug, try to strip the "call log" part for cleaner output.
    if logger.isEnabledFor(logging.DEBUG):
        msg_to_log = full_error_message
    else:
        msg_to_log = full_error_message.split("\nCall log:")[0].strip()

    logger.error("%s: %s", msg, msg_to_log)
    browser.close()
    exit_with_status(1)


def page_load(browser: Browser, page: Page, url: str) -> None:
    """Loads a page using playwright.

    The function will only return if the http response is ok and an exception has
    not occurred, will exit otherwise.

    Args:
        browser (Browser): The playwright browser instance.
        page (Page): The playwright page instance.
        url (str): The URL to load.

    """
    http_code_ok = 200
    try:
        start = time.monotonic()
        resp = page.goto(url, wait_until="networkidle")
        dur = time.monotonic() - start

        if resp is None:
            logger.error(
                "loaded page '%s' in %.2fs, but no http response was returned",
                url,
                dur,
            )
            exit_with_status(1)

        if resp.status != http_code_ok:
            logger.error(
                (
                    "loaded page '%s' in %.2fs seconds, but got unexpected http status"
                    " '%s'"
                ),
                url,
                dur,
                resp.status,
            )
            exit_with_status(1)

        logger.info(
            "loaded page '%s' in %.2f seconds (http %s)",
            url,
            time.monotonic() - start,
            resp.status,
        )

    except PlaywrightError as e:
        log_playwright_error_and_exit(e, f"failed to load url {url}", browser)


def dismiss_cookie_banner(browser: Browser, page: Page) -> None:
    r"""Dismisses the cookie consent banner if present.

    This function attempts to locate and click the 'acceptera alla' button on the page,
    which is used to dismiss the cookie consent banner. If the button is not found or
    visible, a warning is logged, but the program will continue to run as intended, even
    though it will most likely not work properly ¯\_(ツ)_/¯

    Args:
        page (Page): The Playwright Page object representing the current page.
        browser (Browser): The Playwright Browser object representing the current
                           browser.
    """
    accept_all_button = page.query_selector(
        "div.cc-btn.cc-btn-accept.cc-btn-accept-all",
    )

    if accept_all_button:
        logger.debug(
            "found 'acceptera alla' cookie button, attempting to click",
        )
        try:
            accept_all_button.click(timeout=5000)
            page.wait_for_load_state("domcontentloaded")
            logger.debug("cookie consent banner clicked and dismissed")

        except PlaywrightError as e:
            log_playwright_error_and_exit(
                e,
                "failed to load click 'acceptera alla' cookie button",
                browser,
            )
    else:
        logger.warning(
            (
                "no 'acceptera alla' cookie button found or visible, program will"
                "likely not work as intended (bug?)"
            ),
        )


def extract_parking_spaces_from_div(
    rows: list[ElementHandle],
    parking_all: list[Parking],
) -> int:
    """Extracts parking spaces from a list of div elements.

    Args:
        rows: A list of div elements containing parking space information.
        parking_all: A list of dictionaries containing all parking spaces found.
    """
    parkings_found: int = 0
    for row in rows:
        try:
            # Replace non-breaking spaces with standard spaces
            txt = row.inner_text().replace("\\u00a0", " ")

        except PlaywrightError as e:
            logger.warning("could not read text content for element: %s (bug?)", e)
            continue

        if not txt:
            logger.warning("no text content found in element (bug?)")
            continue

        # Must contain all our labels to be considered a parking spot
        found_labels = [label for label in ParkingLabel if label.value in txt]
        missing_labels = [label for label in ParkingLabel if label.value not in txt]

        if missing_labels:
            logger.debug(
                (
                    "element with text '%s' is missing the following labels: '%s',"
                    "found these labels '%s' out of the expected ones"
                ),
                txt,
                ", ".join(missing_labels),
                ", ".join(found_labels),
            )
            continue

        logger.debug(
            (
                "element with text '%s' contains all the %s expected labels, "
                "this should be a parking spot "
            ),
            txt,
            len(found_labels),
        )

        # The 'txt' variable contains the raw text content extracted from the HTML
        # element. This raw text can often have inconsistent formatting, including
        # multiple empty lines, leading/trailing whitespace on lines, and non-breaking
        # spaces.
        #
        # We try to clean this up and making it consistant by by doing the following
        # 1. `txt.splitlines()`: Breaks the entire text block into individual lines.
        # 2. `for line in ...`: Iterates through each of these lines.
        # 3. `line.strip()`: Removes any leading or trailing whitespace from each line.
        # 4. `if line.strip()`: Filters out lines that become empty after stripping
        #    (i.e., lines that were originally empty or only contained whitespace).
        # 5. `"\n".join(...)`: Joins the cleaned, non-empty lines back together
        #    into a single string, with each line separated by a single newline
        #    character.
        #
        # The purpose of this cleanup is to normalize the text 'block' so that
        # the subsequent `extract_field` function can reliably find and extract
        # information using regular expressions, regardless of minor variations
        # in the original HTML's text formatting.
        block = "\n".join(line.strip() for line in txt.splitlines() if line.strip())

        parking = {lbl.name: extract_field(block, lbl.value) for lbl in ParkingLabel}

        if any(parking.values()):
            try:
                parking_obj = Parking.model_validate(parking)
                parking_all.append(parking_obj)
                current_parking = parking_key_identifier(parking_obj.model_dump())
                logger.debug(
                    "created unique parking string '%s' for parking '%s'",
                    current_parking,
                    parking,
                )

                parkings_found += 1
            except ValidationError as e:
                logger.warning(
                    "failed to validate parking space: %s, error: %s",
                    parking,
                    e,
                )

    return parkings_found


def extract_field(block_text: str, label: str) -> str:
    """Extracts the value of a field from a block of text.

    Args:
        block_text: The text block to search within
        label: The label of the field to extract

    Returns:
        The extracted value of the field, or an empty string if not found.
    """
    dbg_block_text = block_text.replace("\n", " ").replace("\r", "")
    logger.debug("trying to extract '%s' from text '%s'", label, dbg_block_text)

    # Search for the label in the block text
    m = re.search(re.escape(label) + r"\s*([^\n\r]+)", block_text)
    if m:
        extracted_value = m.group(1).strip()
        logger.debug(
            "successfully extracted '%s' for label '%s'",
            extracted_value,
            label,
        )
        return extracted_value

    logger.warning("failed to extract '%s' from '%s'", label, dbg_block_text)
    return ""


def parking_key_identifier(parking: dict[str, str]) -> str:
    """Create an identity for a parking item.

    We use the address, area, type, and rent separated by a pipe
    character to create a stable identity for the parking item.

    Args:
        parking(dict[str,str]): The parking item.

    Returns:
        str: The stable identity for the parking item.
    """
    return "|".join(
        parking.get(label.name, "")
        for label in [
            ParkingLabel.area,
            ParkingLabel.address,
            ParkingLabel.kind,
            ParkingLabel.rent,
            ParkingLabel.access,
            ParkingLabel.interest,
        ]
    )


def extract_next_page_el_from_pagination_el(
    pagination_links: list[ElementHandle],
    curr_page: int,
) -> ElementHandle | None:
    """
    Extracts the URL of the next page from the pagination element.

    Args:
        pagination_links: A list of Element objects representing the pagination links.
        curr_page: The current page number.

    Returns:
        The ElementHandle of the next page link, or None if there is no next page.
    """
    # Find the anchor whose visible text equals the next page number
    next_page_num = str(curr_page + 1)

    # Loop through each pagination link
    for span in pagination_links:
        a_el = span.query_selector("a")
        # If the element is not an anchor, skip it (current page)
        if not a_el:
            continue

        # Check if the anchor's text content matches the next page number and
        # return the element
        text = (a_el.text_content() or "").strip()
        if text == next_page_num:
            return span

    return None


def parking_state_load() -> list[Parking]:
    """Loads previously seen parking spaces from a JSON state file.

    Returns:
        list[Parking]: List of previously seen parking spaces.
    """
    parkinglist = TypeAdapter(list[Parking])
    previous_parking_data: list[Parking] = []
    try:
        text = STATE_FILE_PATH.read_text(encoding="utf-8")
        previous_parking_data = parkinglist.validate_json(text)
        logger.debug("previous parking data: %s", previous_parking_data)

        previous_parking_keys = {
            parking_key_identifier(p.model_dump()) for p in previous_parking_data
        }
        logger.info(
            "loaded %s previous parking spaces from state file",
            len(previous_parking_keys),
        )

    except FileNotFoundError:
        logger.debug(
            "parking state file '%s' does not exist, will create",
            STATE_FILE_PATH,
        )
        return []

    except (json.JSONDecodeError, ValidationError) as e:
        logger.warning(
            "corrupted parking state file '%s' (%s), will recreate",
            STATE_FILE_PATH,
            e,
        )
        with contextlib.suppress(OSError):
            STATE_FILE_PATH.unlink(missing_ok=True)
        return []

    except OSError:
        logger.exception(
            "unexpected os error when reading parking state file '%s'",
            STATE_FILE_PATH,
        )
        return []

    return previous_parking_data


def parking_state_save(parking_spaces: list[Parking]) -> None:
    """Saves the current parking spaces to a JSON state file."""
    try:
        # Serialize the sorted set to JSON with pretty-printing (indent=2)
        # and ensure_ascii=False to allow direct UTF-8 characters like åäö.
        _ = STATE_FILE_PATH.write_text(
            json.dumps(
                [p.model_dump() for p in parking_spaces],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        logger.info(
            "saved %s parking spots to file '%s'",
            len(parking_spaces),
            STATE_FILE_PATH,
        )
    except Exception:
        logger.exception("error saving file '%s'", STATE_FILE_PATH)


def notify_via_twilio(message: str) -> None:
    """Sends a message via Twilio.

    Args:
        message: The message to send.
    """
    logger.info("notifying via twilio")

    if WHATSAPP_FROM and WHATSAPP_TO:
        notify_whatsapp(message)


def notify_whatsapp(message: str) -> None:
    """Send a whatsapp notification using twilio api.

    Args:
        message (str): The message to send.

    """
    logger.info("sending whatsapp notification")
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        messages: MessageList = client.messages
        message_instance: MessageInstance = messages.create(
            body=message,
            from_=WHATSAPP_FROM,
            to=WHATSAPP_TO,
        )
        if message_instance.status in ["queued", "sending", "sent", "delivered"]:
            logger.debug(
                "whatsapp notification sent successfully, message sid: %s, status: %s",
                message_instance.sid,
                message_instance.status,
            )
        else:
            logger.error(
                (
                    "whatsapp notification failed, message sid: %s, status: %s, error"
                    "code '%s', error message '%s'"
                ),
                message_instance.sid,
                message_instance.status,
                message_instance.error_code,
                message_instance.error_message,
            )
    except TwilioRestException:
        logger.exception("failure when sending whatsapp notification: ")


def page_scrape(
    browser: Browser,
    page: Page,
    sp: Yaspin,
    parking_all: list[Parking],
) -> None:
    """Scrapes a single page of parking spaces.

    Args:
        browser: The browser instance.
        page: The current page.
        sp: The spinner instance.
        parking_all: The list of all parking spaces.
    """
    curr_page: int = 1
    pagination_links: list[ElementHandle] = []
    rows: list[ElementHandle] = []
    parkings_curr_page: int = 0

    while True:
        logger.debug("scraping page %s", curr_page)

        # Get available parking spaces on current page
        rows = page.query_selector_all("div.objektListaMarknad")

        # No parking spaces found
        if not rows:
            logger.debug("no rows found")
            break

        #  Get pagination links
        pagination_links = page.query_selector_all("span.PaginationList.PageLink")

        if curr_page > 1:
            if is_logging_enabled():
                print(
                    f"{SPINNER_TEXT}, scraping page {curr_page} / ",
                    f"{len(pagination_links)}",
                    f" (a total of {len(parking_all)} parkingspace(s) found)",
                )
            else:
                sp.text = (
                    f"{SPINNER_TEXT}, scraping page {curr_page} / "
                    f"{len(pagination_links)}"
                    f" (a total of {len(parking_all)} parkingspace(s) found)"
                )

        # No pagination links at all
        if not pagination_links:
            logger.warning("no pagination found (bug?)")
        else:
            logger.info("found %s pagination link(s)", len(pagination_links))

        # Extract parking spaces from current page
        parkings_curr_page = extract_parking_spaces_from_div(
            rows,
            parking_all,
        )

        logger.info(
            "found %s unique spaces on page %s / %s",
            parkings_curr_page,
            curr_page,
            len(pagination_links),
        )

        # Extract next page element
        next_page_el = extract_next_page_el_from_pagination_el(
            pagination_links,
            curr_page,
        )

        # Click next page element if it exists
        if next_page_el:
            logger.info("next page found with number %s", curr_page + 1)
            try:
                next_page_el.click(timeout=5000)
                page.wait_for_load_state("networkidle")
                logger.debug(
                    "clicked next page element with number %s",
                    curr_page + 1,
                )
            except PlaywrightError as e:
                log_playwright_error_and_exit(
                    e,
                    f"failed to load click next page with number {curr_page + 1}",
                    browser,
                )
            curr_page += 1
            continue

        # Get here we are done
        break

    # End of pagination loop, scraped all pages
    sp.stop()
    print(
        f"{SPINNER_TEXT}, scraping page {curr_page} /",
        f"{max(1, len(pagination_links))}",
        f"(a total of {len(parking_all)} parkingspace(s) found)",
    )

    logger.debug("no next page found")
    logger.info(
        "found a total of %s unique spaces",
        len(parking_all),
    )


def compare_parkings(
    previous_parkings: list[Parking],
    current_parkings: list[Parking],
) -> dict[str, list[Parking]]:
    """Compare previous and current parkings and return the differences."""
    previous_parking_keys = {
        parking_key_identifier(p.model_dump()) for p in previous_parkings
    }
    current_parking_keys = {
        parking_key_identifier(p.model_dump()) for p in current_parkings
    }

    added_parkings = [
        p
        for p in current_parkings
        if parking_key_identifier(p.model_dump()) not in previous_parking_keys
    ]
    removed_parkings = [
        p
        for p in previous_parkings
        if parking_key_identifier(p.model_dump()) not in current_parking_keys
    ]

    logger.debug("added parkings %s parkings : %s", len(added_parkings), added_parkings)
    logger.debug(
        "removed parkings %s parkings : %s",
        len(removed_parkings),
        removed_parkings,
    )

    return {"added": added_parkings, "removed": removed_parkings}


def construct_and_send_notification(
    parking_differences: dict[str, list[Parking]],
) -> None:
    """Constructs a notification message from and sends it via twilio services.

    Args:
        parking_differences: A dictionary containing added and removed parkings.
    """
    added_parkings = parking_differences.get("added", [])
    removed_parkings = parking_differences.get("removed", [])

    message_parts: list[str] = []

    # Group added parkings by area
    added_by_area: dict[str, list[Parking]] = {}
    for parking in added_parkings:
        area_name = parking.area
        if area_name not in added_by_area:
            added_by_area[area_name] = []
        added_by_area[area_name].append(parking)

    # Group removed parkings by area
    removed_by_area: dict[str, list[Parking]] = {}
    for parking in removed_parkings:
        area_name = parking.area
        if area_name not in removed_by_area:
            removed_by_area[area_name] = []
        removed_by_area[area_name].append(parking)

    message_parts.extend(
        format_parking_grp_msg("Nya parkeringsplatser hittade:", added_by_area),
    )
    message_parts.extend(
        format_parking_grp_msg(
            "Parkeringsplatser ej längre tillgängliga:",
            removed_by_area,
        ),
    )

    if not added_by_area and not removed_by_area:
        logger.info("no changes in the availability of parkings, no message sent")
        return

    # Join message parts with actual newlines (\n) and strip leading/trailing whitespace
    # Filter out any empty strings that might have resulted from conditional formatting
    message = "\n".join(part for part in message_parts if part).strip()
    logger.debug("constructed message '%s' for sending via twilio", message)

    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        max_bytes = 1599
        parts = split_utf8_smart(message, max_bytes, "Antal intresserad")
        for part in parts:
            logger.debug("split message into part '%s' for sending via twilio", part)
            notify_via_twilio(part)


def utf8len(s: str) -> int:
    """Return the length of a string in bytes when encoded as UTF-8.

    Args:
        s: The string to measure.

    Returns:
        The length of the string in bytes when encoded as UTF-8.
    """
    return len(s.encode("utf-8"))


def split_line_hard(line: str, max_bytes: int) -> list[str]:
    """Split a single line into byte-safe chunks.

    Used when a single line > max_bytes

    Args:
        line: The line to split.
        max_bytes: The maximum number of bytes per chunk.

    Returns:
        A list of byte-safe chunks.

    """
    chunks: list[str] = []
    cur = ""
    cur_b = 0
    for ch in line:
        cb = utf8len(ch)
        if cur_b + cb > max_bytes:
            chunks.append(cur)
            cur = ch
            cur_b = cb
        else:
            cur += ch
            cur_b += cb
    if cur:
        chunks.append(cur)
    return chunks


def _is_completion_line(line: str, token: str) -> bool:
    """Look for completion token in line (ignoring simple markdown).

    Args:
        line: The line to search.
        token: The token to look for.

    Returns:
        True if the line contains the token, False otherwise.
    """
    # strip simple markdown emphasis; keep it lightweight
    normalized = line.replace("*", "").replace("_", "").casefold()
    return token.casefold() in normalized


def last_preferred_boundary_bytes(
    cur_text: str,
    completion_token: str,
) -> None | int:
    """Return byte index after the last preferred boundary inside cur_text.

      1) an empty line that is **preceded by** a non-empty line containing
         the completion token, e.g., 'Antal intresserade'),
      2) a line that is exactly '--' (ignoring surrounding whitespace).

    Args:
        cur_text: The text to search.
        completion_token: The token to look for.

    Returns:
        The byte index after the last preferred boundary inside cur_text.
        If none, return None.

    """
    lines = cur_text.splitlines(keepends=True)
    # cumulative UTF-8 byte offsets at line ends
    offsets = [0]
    for ln in lines:
        offsets.append(offsets[-1] + utf8len(ln))

    candidates: list[int] = []
    prev_non_empty: str = ""

    for i, ln in enumerate(lines):
        stripped = ln.strip()

        # Rule 2: explicit separator line '--'
        if stripped == "--":
            candidates.append(offsets[i + 1])  # cut AFTER this line
            # continue scanning, we still want the last candidate

        # Track previous non-empty for Rule 1
        if stripped:
            prev_non_empty = ln
            continue

        # Rule 1: empty line preceded by a completion line
        if prev_non_empty != "" and _is_completion_line(
            prev_non_empty,
            completion_token,
        ):
            candidates.append(offsets[i + 1])  # cut AFTER this empty line

        # Do not update prev_non_empty on empty lines

    return candidates[-1] if candidates else None


def split_utf8_smart(
    text: str,
    max_bytes: int,
    completion_token: str,
) -> list[str]:
    """Split text into UTF-8 byte-limited chunks.

      1) empty line that follows a 'completion' line containing `completion_token`,
      2) a line that equals '--',
      3) otherwise at a safe character boundary.

      Each chunk's UTF-8 length <= max_bytes.

    Args:
        text: The text to split.
        max_bytes: The maximum number of bytes per chunk.
        completion_token: The token to look for in completion lines.

    Returns:
        A list of UTF-8 byte-limited chunks.
    """

    def flush_chunk(buf: list[str], out: list[str]) -> None:
        if buf:
            out.append("".join(buf))
            buf.clear()

    chunks: list[str] = []
    buf: list[str] = []
    buf_b = 0

    for line in text.splitlines(keepends=True):
        lb = utf8len(line)

        if lb > max_bytes:
            # Finish current buffer then hard-split this long line.
            flush_chunk(buf, chunks)
            chunks.extend(split_line_hard(line, max_bytes))
            buf_b = 0
            continue

        # Would this line overflow?
        if buf_b + lb > max_bytes:
            cur_text = "".join(buf)
            cut = last_preferred_boundary_bytes(cur_text, completion_token)

            if cut is not None:
                b = cur_text.encode("utf-8")
                # flush up to cut
                chunks.append(b[:cut].decode("utf-8", errors="strict"))
                remainder = b[cut:].decode("utf-8", errors="strict")
                buf[:] = [remainder] if remainder else []
                buf_b = utf8len(remainder) if remainder else 0

            # Still too big? start a new chunk.
            if buf_b + lb > max_bytes:
                flush_chunk(buf, chunks)
                buf_b = 0

        # add line
        buf.append(line)
        buf_b += lb

    flush_chunk(buf, chunks)
    return chunks


def _get_kind_emoji(kind: str) -> str:
    """Returns an emoji for a given parking kind."""
    return EMOJI_MAP.get(kind.lower(), "")


def _format_single_parking_item(
    parking: Parking,
    *,
    is_last_in_area: bool,
) -> list[str]:
    """Formats a single parking space into a list of message parts."""
    item_parts: list[str] = []
    item_parts.append(f"  *Address:* _{parking.address}_ ")

    kind_emoji = _get_kind_emoji(parking.kind)
    kind_line = f"  *Typ:* _{parking.kind}_ {kind_emoji}"
    item_parts.append(kind_line)

    item_parts.append(f"  *Hyra:* _{parking.rent}_ ")
    item_parts.append(f"  *Tillträde:* _{parking.access}_ ")
    item_parts.append(f"  *Antal intresserade:* _{parking.interest}_ ")

    if not is_last_in_area:
        item_parts.append("  -- ")
    return item_parts


def format_parking_grp_msg(
    title: str,
    parking_group: dict[str, list[Parking]],
) -> list[str]:
    """Helper to format a group of parking spaces for notification."""
    parts: list[str] = []
    if not parking_group:
        return parts

    # Add the title followed by a newline as per the desired format
    parts.append(f"{title}")

    # Sort areas for consistent output
    for area_name in sorted(parking_group.keys()):
        # Bold area name with two preceding newlines as per the desired format
        parts.append(f"\n*{area_name}*\n")

        # Sort parkings within each area by address, then kind, rent, access,
        # interest
        sorted_parkings_in_area = sorted(
            parking_group[area_name],
            key=lambda p: (
                p.address.lower(),
                p.kind.lower(),
                p.rent.lower(),
                p.access.lower(),
                p.interest.lower(),
            ),
        )

        for i, parking in enumerate(sorted_parkings_in_area):
            is_last = i == len(sorted_parkings_in_area) - 1
            parts.extend(_format_single_parking_item(parking, is_last_in_area=is_last))
        parts.append("")  # Add an empty line after each area group for separation

    return parts


def print_results(parking_all: list[Parking]) -> None:
    """
    Print the results of available parkings.

    Args:
        parking_all: List of all parkings.

    """
    # Output table
    table_data: list[list[str]] = []
    if parking_all:
        # Sort the spots by the area, address, kind, rent, interest and access
        parking_all.sort(
            key=lambda p: (
                p.area.lower(),
                p.address.lower(),
                p.kind.lower(),
                p.rent.lower(),
                p.interest.lower(),
                p.access.lower(),
            ),
        )

        # Define the desired display headers as a list, matching the order of data
        # extraction.
        display_headers = [
            ParkingLabel.area.name,
            ParkingLabel.address.name,
            ParkingLabel.kind.name,
            ParkingLabel.rent.name,
            ParkingLabel.access.name,
            ParkingLabel.interest.name,
        ]

        # Extract data in the correct order for tabulate
        for p in parking_all:
            parking_space = [
                p.area,
                p.address,
                p.kind,
                p.rent,
                p.access,
                p.interest,
            ]
            table_data.append(parking_space)

        # Print the table to stdout
        print(
            tabulate(
                table_data,
                headers=display_headers,
                tablefmt="fancy_grid",
                stralign="left",
            ),
        )
    else:
        logger.info("no parking spots to display in table format")


def is_logging_enabled() -> bool:
    """Check if logging is enabled.

    Returns:
        bool: True if logging is enabled, False otherwise.

    """
    return logging.getLogger().isEnabledFor(logging.CRITICAL)


def sig_handler_yaspin(
    _signum: int,
    _frame: FrameType,
    spinner: Yaspin,
    browser: Browser | None,
) -> None:
    """Custom signal handler for SIGINT.

    Args:
        _signum (int): Signal number.
        _frame (FrameType): Current stack frame.
        spinner (Yaspin): Spinner instance.
        browser (Browser): Holds the browser instance.
    """
    if isinstance(browser, Browser):
        browser.close()
    spinner.red.fail("-> received SIGINT ")
    spinner.stop()
    sys.exit(1)


# def handle_sigint(_signum: int, _frame: FrameType) -> None:
#    """Custom signal handler for SIGINT.
#
#    Args:
#        _signum (int): Signal number.
#        _frame (FrameType): Current stack frame.
#    """
#    if isinstance(browser, Browser):
#        browser.close()
#    print("\nCaught SIGINT (Ctrl+C). Cleaning up...")
#    # do cleanup code here
#    sys.exit(0)  # or raise KeyboardInterrupt()
import threading

stop = threading.Event()


def sig_handler(
    _signum: int,
    _frame: FrameType | None,
    *,
    browser: Browser | None,
    page: Page | None,
) -> None:
    """Custom signal handler for SIGINT.

    Args:
        _signum (int): Signal number.
        _frame (FrameType): Current stack frame.
        browser (Browser): Holds the browser instance.
    """
    print("\nSIGINT received")
    # if isinstance(page, Page):
    #    print("\nSIGINT received — closing page…")
    #    page.close()
    # if isinstance(browser, Browser):
    #    print("\nSIGINT received — closing browser")
    #    browser.close()

    # stop.set()
    raise SystemExit(130)

    # sys.exit(130)


def install_signal_handlers(
    browser: Browser | None,
    page: Page | None,
) -> Callable[[int, FrameType | None], None]:
    """
    Install signal handlers for SIGINT and SIGTERM.

    Args:
        browser (Browser): Holds the browser instance.
        page (Page): Holds the page instance.
    """

    def handler(_signum: int, _frame: FrameType | None) -> None:
        stop.set()
        signal.default_int_handler(_signum, _frame)

    _ = signal.signal(signal.SIGINT, handler)
    with contextlib.suppress(AttributeError):
        _ = signal.signal(signal.SIGTERM, handler)
    return handler  # handy for yaspin.sigmap

    # sig_handler(_signum, _frame, browser=browser, page=page)
    # Always available
    # _ = signal.signal(signal.SIGINT, handler)
    # On platforms where SIGTERM doesn't exist (Windows)
    # with contextlib.suppress(AttributeError):
    #    _ = signal.signal(signal.SIGTERM, handler)
    # return handler


def main() -> None:
    """Main entry point of the program."""
    # Configure global logger instance
    setup_logging(os.getenv("LOG_LEVEL", ""))
    browser: Browser | None = None
    page: Page | None = None
    _ = install_signal_handlers(browser, page)

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception(
            "failed to create cache directory '%s', check permissions or disk space",
            CACHE_DIR,
        )
        exit_with_status(1)

    parking_previous = parking_state_load()

    # First argument is the area code
    areas = sys.argv[1:]

    # Validate, display areas and build url
    areas_parsed = validate_and_display_areas(areas)

    print("Will search for parking spaces in the following areas:")
    if len(areas_parsed) == 0:
        for area, name in AREA_MAP.items():
            print(f" » {name} ({area})")
    else:
        for area in areas_parsed:
            print(f" » {AREA_MAP[area]} ({area})")

    url = build_url(areas_parsed)
    logger.debug("scraping url '%s' with playwright", url)

    # Initialize spinner with sigmap including browser
    sp = yaspin(
        text=f"{SPINNER_TEXT}, scraping page 1",
        side="right",
        sigmap={
            signal.SIGINT: functools.partial(
                sig_handler_yaspin,
                browser=browser,
            ),
        },
    )

    # Setup browser context
    with sync_playwright() as ctx:
        # Launch browser
        browser = ctx.chromium.launch(headless=True)

        if is_logging_enabled():
            print(
                f"{SPINNER_TEXT}, scraping page 1",
            )
        else:
            sp.start()

        # Variables for parking spaces
        parking_all: list[Parking] = []
        parking_changes: dict[str, list[Parking]] = {"added": [], "removed": []}

        # Create a new page, navigate to url and dismiss cookie banner
        page = browser.new_page()
        try:
            while not stop.is_set():
                page_load(browser, page, url)
                dismiss_cookie_banner(browser, page)

                page_scrape(browser, page, sp, parking_all)
                parking_changes = compare_parkings(parking_previous, parking_all)
        except KeyboardInterrupt:
            return 130
        finally:
            print("closing")
            # Close quietly, in order; ignore harmless races
            with contextlib.suppress(PlaywrightError):
                if page and not page.is_closed():
                    page.close()
            with contextlib.suppress(PlaywrightError):
                if browser and browser.is_connected():
                    browser.close()
    # Save the current state
    parking_state_save(parking_all)

    # Print results
    print_results(parking_all)

    # Print changes
    construct_and_send_notification(parking_changes)


if __name__ == "__main__":
    main()
