"""Portal registry (blueprint §5). Every entry is data, so URLs/adapters can be recalibrated
without code changes: set PORTAL_REGISTRY_FILE to a JSON list of PortalConfig dicts to override.

``verified=False`` means the listing URL/parse heuristics have not yet been calibrated against the
live site from a network that can reach it; those portals still run, and failures are reported per
portal in the harvest summary rather than aborting the run.
"""
from __future__ import annotations

import json
import os

from .base import PortalConfig

N, M, S, E, P = "NATIONAL_PROVINCIAL", "MUNICIPAL", "SOE", "SETA", "PRIVATE"

DEFAULT_PORTALS: list[PortalConfig] = [
    PortalConfig("etenders", "National Treasury eTenders (OCDS API)", N,
                 "https://ocds-api.etenders.gov.za/api/OCDSReleases", "ocds", "National Treasury"),
    PortalConfig("gp_treasury", "Gauteng Provincial Treasury", N, "https://www.treasury.gpg.gov.za/Pages/tenders.aspx", "playwright", "Gauteng Provincial Government", "Gauteng"),
    PortalConfig("wc_treasury", "Western Cape Government Procurement", N, "https://www.westerncape.gov.za/tenders", "html", "Western Cape Government", "Western Cape"),
    PortalConfig("kzn_treasury", "KwaZulu-Natal Provincial Treasury", N, "https://www.kzntreasury.gov.za/tenders", "html", "KZN Provincial Treasury", "KwaZulu-Natal"),
    PortalConfig("ec_treasury", "Eastern Cape Provincial Treasury", N, "https://www.ectreasury.gov.za/tenders", "html", "EC Provincial Treasury", "Eastern Cape"),
    PortalConfig("coj", "City of Johannesburg", M, "https://www.joburg.org.za/work_/TendersQuotations/Pages/Tenders.aspx", "playwright", "City of Johannesburg", "Gauteng"),
    PortalConfig("tshwane", "City of Tshwane", M, "https://www.tshwane.gov.za/?page_id=2194", "playwright", "City of Tshwane", "Gauteng"),
    PortalConfig("cape_town", "City of Cape Town", M, "https://web1.capetown.gov.za/web1/procurementportal/", "playwright", "City of Cape Town", "Western Cape"),
    PortalConfig("ethekwini", "eThekwini Municipality", M, "https://www.durban.gov.za/pages/business/procurement", "playwright", "eThekwini Municipality", "KwaZulu-Natal"),
    PortalConfig("ekurhuleni", "City of Ekurhuleni", M, "https://www.ekurhuleni.gov.za/tenders/", "html", "City of Ekurhuleni", "Gauteng"),
    PortalConfig("eskom", "Eskom Tender Bulletin", S, "https://tenderbulletin.eskom.co.za/", "playwright", "Eskom Holdings SOC Ltd"),
    PortalConfig("transnet", "Transnet eTenders", S, "https://transnetetenders.azurewebsites.net/Home/AdvertisedTenders", "playwright", "Transnet SOC Ltd"),
    PortalConfig("sanral", "SANRAL", S, "https://www.nra.co.za/sanral-tenders/list/open-tenders", "playwright", "SANRAL SOC Ltd"),
    PortalConfig("rand_water", "Rand Water", S, "https://www.randwater.co.za/tenders.php", "html", "Rand Water"),
    PortalConfig("prasa", "PRASA", S, "https://www.prasa.com/Tenders.aspx", "playwright", "PRASA"),
    PortalConfig("telkom", "Telkom", S, "https://www.telkom.co.za/about_us/procurement/tenders", "playwright", "Telkom SA SOC Ltd"),
    PortalConfig("ceta", "CETA (Construction SETA)", E, "https://www.ceta.org.za/tenders/", "html", "CETA"),
    PortalConfig("ewseta", "EWSETA (Energy & Water SETA)", E, "https://www.ewseta.org.za/tenders", "html", "EWSETA"),
    PortalConfig("merseta", "MERSETA", E, "https://www.merseta.org.za/tenders/", "html", "MERSETA"),
    PortalConfig("mict_seta", "MICT SETA", E, "https://www.mict.org.za/tenders/", "html", "MICT SETA"),
    PortalConfig("services_seta", "Services SETA", E, "https://www.serviceseta.org.za/tenders", "html", "Services SETA"),
    PortalConfig("hwseta", "HWSETA", E, "https://www.hwseta.org.za/tenders/", "html", "HWSETA"),
    PortalConfig("sasol", "Sasol Supplier Portal", P, "https://www.sasol.com/suppliers/opportunities", "playwright", "Sasol Ltd"),
    PortalConfig("anglo_american", "Anglo American Supplier Opportunities", P, "https://www.angloamerican.com/suppliers", "playwright", "Anglo American"),
    PortalConfig("sibanye", "Sibanye-Stillwater Supplier Portal", P, "https://www.sibanyestillwater.com/business/suppliers/", "playwright", "Sibanye-Stillwater"),
    PortalConfig("shoprite", "Shoprite Supplier Portal", P, "https://www.shopriteholdings.co.za/suppliers.html", "playwright", "Shoprite Holdings"),
    PortalConfig("massmart", "Massmart Supplier Portal", P, "https://www.massmart.co.za/suppliers/", "playwright", "Massmart"),
]


def load_portals() -> list[PortalConfig]:
    path = os.environ.get("PORTAL_REGISTRY_FILE")
    if path and os.path.exists(path):
        with open(path) as fh:
            return [PortalConfig(**p) for p in json.load(fh)]
    return list(DEFAULT_PORTALS)


def portals_by_key() -> dict[str, PortalConfig]:
    return {p.key: p for p in load_portals()}
