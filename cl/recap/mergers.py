# Code for merging PACER content into the DB
import json
import logging
import re
from copy import deepcopy
from datetime import date, timedelta
from typing import Any

from asgiref.sync import async_to_sync, sync_to_async
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Count, Prefetch, Q, QuerySet
from django.utils.timezone import now
from juriscraper.lib.string_utils import CaseNameTweaker
from juriscraper.pacer import AppellateAttachmentPage, AttachmentPage

from cl.alerts.utils import (
    set_skip_percolation_if_bankruptcy_data,
    set_skip_percolation_if_parties_data,
)
from cl.corpus_importer.utils import (
    ais_appellate_court,
    is_long_appellate_document_number,
    mark_ia_upload_needed,
)
from cl.lib.decorators import retry
from cl.lib.filesizes import convert_size_to_bytes
from cl.lib.model_helpers import clean_docket_number, make_docket_number_core
from cl.lib.pacer import (
    get_blocked_status,
    map_cl_to_pacer_id,
    map_pacer_to_cl_id,
    normalize_attorney_contact,
    normalize_attorney_role,
)
from cl.lib.privacy_tools import anonymize
from cl.lib.timezone_helpers import localize_date_and_time
from cl.lib.utils import previous_and_next, remove_duplicate_dicts
from cl.people_db.lookup_utils import lookup_judge_by_full_name_and_set_attr
from cl.people_db.models import (
    Attorney,
    AttorneyOrganization,
    AttorneyOrganizationAssociation,
    CriminalComplaint,
    CriminalCount,
    Party,
    PartyType,
    Role,
)
from cl.recap.constants import bankruptcy_data_fields
from cl.recap.models import (
    PROCESSING_STATUS,
    UPLOAD_TYPE,
    PacerHtmlFiles,
    ProcessingQueue,
)
from cl.search.models import (
    BankruptcyInformation,
    Claim,
    ClaimHistory,
    Court,
    Docket,
    DocketEntry,
    OriginatingCourtInformation,
    RECAPDocument,
    Tag,
)
from cl.search.tasks import index_docket_parties_in_es

logger = logging.getLogger(__name__)

cnt = CaseNameTweaker()


def confirm_docket_number_core_lookup_match(
    docket: Docket,
    docket_number: str,
    federal_defendant_number: str | None = None,
    federal_dn_judge_initials_assigned: str | None = None,
    federal_dn_judge_initials_referred: str | None = None,
) -> Docket | None:
    """Confirm if the docket_number_core lookup match returns the right docket
    by confirming the docket_number and docket_number components also matches
    if they're available.

    :param docket: The docket matched by the lookup
    :param docket_number: The incoming docket_number to lookup.
    :param federal_defendant_number: The federal defendant number to validate
    the match.
    :param federal_dn_judge_initials_assigned: The judge's initials assigned to
    validate the match.
    :param federal_dn_judge_initials_referred: The judge's initials referred to
    validate the match.
    :return: The docket object if both dockets matched or otherwise None.
    """
    existing_docket_number = clean_docket_number(docket.docket_number)
    incoming_docket_number = clean_docket_number(docket_number)
    if existing_docket_number != incoming_docket_number:
        return None

    # If the incoming data contains docket_number components and the docket
    # also contains DN components, use them to confirm that the docket matches.
    dn_components = {
        "federal_defendant_number": federal_defendant_number,
        "federal_dn_judge_initials_assigned": federal_dn_judge_initials_assigned,
        "federal_dn_judge_initials_referred": federal_dn_judge_initials_referred,
    }
    # Only compare DN component values if both the incoming data and the docket contain
    # non-None DN component values.
    for dn_key, dn_value in dn_components.items():
        incoming_dn_value = dn_value
        docket_dn_value = getattr(docket, dn_key, None)
        if (
            incoming_dn_value
            and docket_dn_value
            and incoming_dn_value != docket_dn_value
        ):
            return None
    return docket


async def find_docket_object(
    court_id: str,
    pacer_case_id: str | None,
    docket_number: str,
    federal_defendant_number: str | None,
    federal_dn_judge_initials_assigned: str | None,
    federal_dn_judge_initials_referred: str | None,
    using: str = "default",
) -> Docket:
    """Attempt to find the docket based on the parsed docket data. If cannot be
    found, create a new docket. If multiple are found, return the oldest.

    :param court_id: The CourtListener court_id to lookup
    :param pacer_case_id: The PACER case ID for the docket
    :param docket_number: The docket number to lookup.
    :param federal_defendant_number: The federal defendant number to validate
    the match.
    :param federal_dn_judge_initials_assigned: The judge's initials assigned to
    validate the match.
    :param federal_dn_judge_initials_referred: The judge's initials referred to
    validate the match.
    :param using: The database to use for the lookup queries.
    :return The docket found or created.
    """
    # Attempt several lookups of decreasing specificity. Note that
    # pacer_case_id is required for Docket and Docket History uploads.
    d = None
    docket_number_core = make_docket_number_core(docket_number)
    lookups = []
    if pacer_case_id:
        # Appellate RSS feeds don't contain a pacer_case_id, avoid lookups by
        # blank pacer_case_id values.
        if docket_number_core:
            # Only do these if docket_number_core is not blank. See #5058.
            lookups.extend(
                [
                    {
                        "pacer_case_id": pacer_case_id,
                        "docket_number_core": docket_number_core,
                    },
                    # Appellate docket uploads usually include a pacer_case_id.
                    # Therefore, include the following lookup to attempt matching
                    # existing dockets without a pacer_case_id using docket_number_core
                    # to avoid creating duplicated dockets.
                    {
                        "pacer_case_id": None,
                        "docket_number_core": docket_number_core,
                    },
                ]
            )
        lookups.append({"pacer_case_id": pacer_case_id})
    if docket_number_core and not pacer_case_id:
        # Sometimes we don't know how to make core docket numbers. If that's
        # the case, we will have a blank value for the field. We must not do
        # lookups by blank values. See: freelawproject/courtlistener#1531
        lookups.extend(
            [
                {
                    "pacer_case_id": None,
                    "docket_number_core": docket_number_core,
                },
                {"docket_number_core": docket_number_core},
            ]
        )
    elif docket_number and not pacer_case_id:
        # Finally, as a last resort, we can try the docket number. It might not
        # match b/c of punctuation or whatever, but we can try. Avoid lookups
        # by blank docket_number values.
        lookups.append(
            {"pacer_case_id": None, "docket_number": docket_number},
        )

    for kwargs in lookups:
        ds = Docket.objects.filter(court_id=court_id, **kwargs).using(using)
        count = await ds.acount()
        if count == 0:
            continue  # Try a looser lookup.
        if count == 1:
            d = await ds.afirst()
            if kwargs.get("pacer_case_id") is None and kwargs.get(
                "docket_number_core"
            ):
                d = confirm_docket_number_core_lookup_match(
                    d,
                    docket_number,
                    federal_defendant_number,
                    federal_dn_judge_initials_assigned,
                    federal_dn_judge_initials_referred,
                )
            if d:
                break  # Nailed it!
        elif count > 1:
            # If more than one docket matches, try refining the results using
            # available docket_number components.
            dn_components = {
                "federal_defendant_number": federal_defendant_number,
                "federal_dn_judge_initials_assigned": federal_dn_judge_initials_assigned,
                "federal_dn_judge_initials_referred": federal_dn_judge_initials_referred,
            }
            dn_lookup = {
                dn_key: dn_value
                for dn_key, dn_value in dn_components.items()
                if dn_value
            }
            dn_queryset = ds.filter(**dn_lookup).using(using)
            count = await dn_queryset.acount()
            if count == 1:
                d = await dn_queryset.afirst()
            else:
                # Choose the oldest one and live with it.
                d = await ds.aearliest("date_created")
                if kwargs.get("pacer_case_id") is None and kwargs.get(
                    "docket_number_core"
                ):
                    d = confirm_docket_number_core_lookup_match(
                        d, docket_number
                    )
            if d:
                break
    if d is None:
        # Couldn't find a docket. Return a new one.
        return Docket(
            source=Docket.RECAP,
            pacer_case_id=pacer_case_id,
            court_id=court_id,
        )

    if using != "default":
        # Get the item from the default DB
        d = await Docket.objects.aget(pk=d.pk)

    return d


def add_attorney(atty, p, d):
    """Add/update an attorney.

    Given an attorney node, and a party and a docket object, add the attorney
    to the database or link the attorney to the new docket. Also add/update the
    attorney organization, and the attorney's role in the case.

    :param atty: A dict representing an attorney, as provided by Juriscraper.
    :param p: A Party object
    :param d: A Docket object
    :return: None if there's an error, or an Attorney ID if not.
    """
    atty_org_info, atty_info = normalize_attorney_contact(
        atty["contact"], fallback_name=atty["name"]
    )

    # Try lookup by atty name in the docket.
    attys = Attorney.objects.filter(
        name=atty["name"], roles__docket=d
    ).distinct()
    count = attys.count()
    if count == 0:
        # Couldn't find the attorney. Make one.
        a = Attorney.objects.create(
            name=atty["name"], contact_raw=atty["contact"]
        )
    elif count == 1:
        # Nailed it.
        a = attys[0]
    elif count >= 2:
        # Too many found, choose the most recent attorney.
        logger.info(
            "Got too many results for atty: '%s'. Picking earliest.", atty
        )
        a = attys.earliest("date_created")

    # Associate the attorney with an org and update their contact info.
    if atty["contact"]:
        if atty_org_info:
            try:
                org = AttorneyOrganization.objects.get(
                    lookup_key=atty_org_info["lookup_key"],
                )
            except AttorneyOrganization.DoesNotExist:
                try:
                    org = AttorneyOrganization.objects.create(**atty_org_info)
                except IntegrityError:
                    # Race condition. Item was created after get. Try again.
                    org = AttorneyOrganization.objects.get(
                        lookup_key=atty_org_info["lookup_key"],
                    )

            # Add the attorney to the organization
            AttorneyOrganizationAssociation.objects.get_or_create(
                attorney=a, attorney_organization=org, docket=d
            )

        if atty_info:
            a.contact_raw = atty["contact"]
            a.email = atty_info["email"]
            a.phone = atty_info["phone"]
            a.fax = atty_info["fax"]
            a.save()

    # Do roles
    roles = atty["roles"]
    if len(roles) == 0:
        roles = [{"role": Role.UNKNOWN, "date_action": None}]

    # Delete the old roles, replace with new.
    Role.objects.filter(attorney=a, party=p, docket=d).delete()
    Role.objects.bulk_create(
        [
            Role(attorney=a, party=p, docket=d, **atty_role)
            for atty_role in roles
        ]
    )
    return a.pk


def update_case_names(d, new_case_name):
    """Update the case name fields if applicable.

    This is a more complex than you'd think at first, and has resulted in at
    least two live bugs. The existing dockets and the new data can each have
    one of three values:

     - A valid case name
     - Unknown Case Title (UCT)
     - ""

    So here's a matrix for what to do:

                                       new_case_name
                       +------------+-----------------+-----------------+
                       |   x v. y   |      UCT        |      blank      |
             +---------+------------+-----------------+-----------------+
             | x v. y  |   Update   |    No update    |    No update    |
             +---------+------------+-----------------+-----------------+
      docket |  UCT    |   Update   |  Same/Whatever  |    No update    |
             +---------+------------+-----------------+-----------------+
             |  blank  |   Update   |     Update      |  Same/Whatever  |
             +---------+------------+-----------------+-----------------+

    :param d: The docket object to update or ignore
    :param new_case_name: The incoming case name
    :returns d
    """
    uct = "Unknown Case Title"
    if not new_case_name:
        return d
    if new_case_name == uct and d.case_name != "":
        return d

    d.case_name = new_case_name
    d.case_name_short = cnt.make_case_name_short(d.case_name)
    return d


async def update_docket_metadata(
    d: Docket, docket_data: dict[str, Any]
) -> Docket:
    """Update the Docket object with the data from Juriscraper.

    Works on either docket history report or docket report (appellate
    or district) results.
    """
    d = update_case_names(d, docket_data["case_name"])
    await mark_ia_upload_needed(d, save_docket=False)
    d.docket_number = docket_data["docket_number"] or d.docket_number
    d.pacer_case_id = d.pacer_case_id or docket_data.get("pacer_case_id")
    d.date_filed = docket_data.get("date_filed") or d.date_filed
    d.date_last_filing = (
        docket_data.get("date_last_filing") or d.date_last_filing
    )
    d.date_terminated = docket_data.get("date_terminated") or d.date_terminated
    d.cause = docket_data.get("cause") or d.cause
    # Avoid updating the nature_of_suit if the docket already has a
    # nature_of_suit set, since this value doesn't change. See issue #3878.
    d.nature_of_suit = d.nature_of_suit or docket_data.get(
        "nature_of_suit", ""
    )
    d.jury_demand = docket_data.get("jury_demand") or d.jury_demand
    d.jurisdiction_type = (
        docket_data.get("jurisdiction") or d.jurisdiction_type
    )
    d.mdl_status = docket_data.get("mdl_status") or d.mdl_status
    await lookup_judge_by_full_name_and_set_attr(
        d,
        "assigned_to",
        docket_data.get("assigned_to_str"),
        d.court_id,
        docket_data.get("date_filed"),
    )
    d.assigned_to_str = docket_data.get("assigned_to_str") or d.assigned_to_str
    await lookup_judge_by_full_name_and_set_attr(
        d,
        "referred_to",
        docket_data.get("referred_to_str"),
        d.court_id,
        docket_data.get("date_filed"),
    )
    d.referred_to_str = docket_data.get("referred_to_str") or d.referred_to_str
    d.blocked, d.date_blocked = await get_blocked_status(d)

    # Update docket_number components:
    d.federal_dn_office_code = (
        docket_data.get("federal_dn_office_code") or d.federal_dn_office_code
    )
    d.federal_dn_case_type = (
        docket_data.get("federal_dn_case_type") or d.federal_dn_case_type
    )
    d.federal_dn_judge_initials_assigned = (
        docket_data.get("federal_dn_judge_initials_assigned")
        or d.federal_dn_judge_initials_assigned
    )
    d.federal_dn_judge_initials_referred = (
        docket_data.get("federal_dn_judge_initials_referred")
        or d.federal_dn_judge_initials_referred
    )
    d.federal_defendant_number = (
        docket_data.get("federal_defendant_number")
        or d.federal_defendant_number
    )

    return d


async def update_docket_appellate_metadata(d, docket_data):
    """Update the metadata specific to appellate cases."""
    if not any(
        [
            docket_data.get("originating_court_information"),
            docket_data.get("appeal_from"),
            docket_data.get("panel"),
        ]
    ):
        # Probably not appellate.
        return d, None

    d.panel_str = ", ".join(docket_data.get("panel", [])) or d.panel_str
    d.appellate_fee_status = (
        docket_data.get("fee_status", "") or d.appellate_fee_status
    )
    d.appellate_case_type_information = (
        docket_data.get("case_type_information", "")
        or d.appellate_case_type_information
    )
    d.appeal_from_str = docket_data.get("appeal_from", "") or d.appeal_from_str

    # Do originating court information dict
    og_info = docket_data.get("originating_court_information")
    if not og_info:
        return d, None

    if og_info.get("court_id"):
        cl_id = map_pacer_to_cl_id(og_info["court_id"])
        if await Court.objects.filter(pk=cl_id).aexists():
            # Ensure the court exists. Sometimes PACER does weird things,
            # like in 14-1743 in CA3, where it says the court_id is 'uspci'.
            # If we don't do this check, the court ID could be invalid, and
            # our whole save of the docket fails.
            d.appeal_from_id = cl_id

    if d.pk is None:
        d_og_info = OriginatingCourtInformation()
    else:
        try:
            d_og_info = await OriginatingCourtInformation.objects.aget(
                docket=d
            )
        except OriginatingCourtInformation.DoesNotExist:
            d_og_info = OriginatingCourtInformation()

    # Ensure we don't share A-Numbers, which can sometimes be in the docket
    # number field.
    docket_number = og_info.get("docket_number", "") or d_og_info.docket_number
    docket_number, _ = anonymize(docket_number)
    d_og_info.docket_number = docket_number
    d_og_info.court_reporter = (
        og_info.get("court_reporter", "") or d_og_info.court_reporter
    )
    d_og_info.date_disposed = (
        og_info.get("date_disposed") or d_og_info.date_disposed
    )
    d_og_info.date_filed = og_info.get("date_filed") or d_og_info.date_filed
    d_og_info.date_judgment = (
        og_info.get("date_judgment") or d_og_info.date_judgment
    )
    d_og_info.date_judgment_eod = (
        og_info.get("date_judgment_eod") or d_og_info.date_judgment_eod
    )
    d_og_info.date_filed_noa = (
        og_info.get("date_filed_noa") or d_og_info.date_filed_noa
    )
    d_og_info.date_received_coa = (
        og_info.get("date_received_coa") or d_og_info.date_received_coa
    )
    d_og_info.assigned_to_str = (
        og_info.get("assigned_to") or d_og_info.assigned_to_str
    )
    d_og_info.ordering_judge_str = (
        og_info.get("ordering_judge") or d_og_info.ordering_judge_str
    )

    if not all([d.appeal_from_id, d_og_info.date_filed]):
        # Can't do judge lookups. Call it quits.
        return d, d_og_info

    await lookup_judge_by_full_name_and_set_attr(
        d_og_info,
        "assigned_to",
        og_info.get("assigned_to"),
        d.appeal_from_id,
        d_og_info.date_filed,
    )
    await lookup_judge_by_full_name_and_set_attr(
        d_og_info,
        "ordering_judge",
        og_info.get("ordering_judge"),
        d.appeal_from_id,
        d_og_info.date_filed,
    )

    return d, d_og_info


def get_order_of_docket(docket_entries):
    """Determine whether the docket is ascending or descending or whether
    that is knowable.
    """
    order = None
    for _, de, nxt in previous_and_next(docket_entries):
        try:
            current_num = int(de["document_number"])
            nxt_num = int(nxt["document_number"])
        except (TypeError, ValueError):
            # One or the other can't be cast to an int. Continue until we have
            # two consecutive ints we can compare.
            continue

        if current_num == nxt_num:
            # Not sure if this is possible. No known instances in the wild.
            continue
        elif current_num < nxt_num:
            order = "asc"
        elif current_num > nxt_num:
            order = "desc"
        break
    return order


def make_recap_sequence_number(
    date_filed: date, recap_sequence_index: int
) -> str:
    """Make a sequence number using a date and index.

    :param date_filed: The entry date_filed used to make the sequence number.
    :param recap_sequence_index: This index will be used to populate the
    returned sequence number.
    :return: A str to use as the recap_sequence_number
    """
    template = "%s.%03d"
    return template % (
        date_filed.isoformat(),
        recap_sequence_index,
    )


def calculate_recap_sequence_numbers(docket_entries: list, court_id: str):
    """Figure out the RECAP sequence number values for docket entries
    returned by a parser.

    Writ large, this is pretty simple, but for some items you need to perform
    disambiguation using neighboring docket entries. For example, if you get
    the following docket entries, you need to use the neighboring items to
    figure out which is first:

           Date     | No. |  Description
        2014-01-01  |     |  Some stuff
        2014-01-01  |     |  More stuff
        2014-01-02  |  1  |  Still more

    For those first two items, you have the date, but that's it. No numbers,
    no de_seqno, no nuthin'. The way to handle this is to start by ensuring
    that the docket is in ascending order and correct it if not. With that
    done, you can use the values of the previous items to sort out each item
    in turn.

    :param docket_entries: A list of docket entry dicts from juriscraper or
    another parser containing information about docket entries for a docket
    :param court_id: The court id to which docket entries belong, used for
    timezone conversion.
    :return None, but sets the recap_sequence_number for all items.
    """
    # Determine the sort order of the docket entries and normalize it
    order = get_order_of_docket(docket_entries)
    if order == "desc":
        docket_entries.reverse()

    # Assign sequence numbers
    for prev, de, _ in previous_and_next(docket_entries):
        current_date_filed, current_time_filed = localize_date_and_time(
            court_id, de["date_filed"]
        )
        prev_date_filed = None
        if prev is not None:
            prev_date_filed, prev_time_filed = localize_date_and_time(
                court_id, prev["date_filed"]
            )
        if prev is not None and current_date_filed == prev_date_filed:
            # Previous item has same date. Increment the sequence number.
            de["recap_sequence_index"] = prev["recap_sequence_index"] + 1
            de["recap_sequence_number"] = make_recap_sequence_number(
                current_date_filed, de["recap_sequence_index"]
            )
            continue
        else:
            # prev is None --> First item on the list; OR
            # current is different than previous --> Changed date.
            # Take same action: Reset the index & assign it.
            de["recap_sequence_index"] = 1
            de["recap_sequence_number"] = make_recap_sequence_number(
                current_date_filed, de["recap_sequence_index"]
            )
            continue

    # Cleanup
    [de.pop("recap_sequence_index", None) for de in docket_entries]


def normalize_long_description(docket_entry):
    """Ensure that the docket entry description is normalized

    This is important because the long descriptions from the DocketHistory
    report and the Docket report vary, with the latter appending something like
    "(Entered: 01/01/2014)" on the end of every entry. Having this value means
    that our merging algorithms fail since the *only* unique thing we have for
    a unnumbered minute entry is the description itself.

    :param docket_entry: The scraped dict from Juriscraper for the docket
    entry.
    :return None (the item is modified in place)
    """
    if not docket_entry.get("description"):
        return

    # Remove the entry info from the end of the long descriptions
    desc = docket_entry["description"]
    desc = re.sub(r"(.*) \(Entered: .*\)$", r"\1", desc)

    # Remove any brackets around numbers (this happens on the DHR long
    # descriptions).
    desc = re.sub(r"\[(\d+)\]", r"\1", desc)

    docket_entry["description"] = desc


async def merge_unnumbered_docket_entries(
    des: QuerySet, docket_entry: dict[str, any]
) -> DocketEntry:
    """Unnumbered docket entries come from many sources, with different data.
    This sometimes results in two docket entries when there should be one. The
    docket history report is the one source that sometimes has the long and
    the short descriptions. When this happens, we have an opportunity to put
    them back together again, deleting the duplicate items.

    :param des: A QuerySet of DocketEntries that we believe are the same.
    :param docket_entry: The scraped dict from Juriscraper for the docket
    entry.
    :return The winning DocketEntry
    """

    # Look for docket entries that match by equal long description or if the
    # long description is not set.
    matched_entries_queryset = des.filter(
        Q(description=docket_entry["description"]) | Q(description="")
    )
    if await matched_entries_queryset.aexists():
        # Return the entry that matches the long description and remove the
        # rest if there are any duplicates.
        winner = await matched_entries_queryset.aearliest("date_created")
        await matched_entries_queryset.exclude(pk=winner.pk).adelete()
        return winner

    # No duplicates found by long description, choose the earliest as the
    # winner; delete the rest
    winner = await des.aearliest("date_created")
    await des.exclude(pk=winner.pk).adelete()
    return winner


@sync_to_async
def add_create_docket_entry_transaction(d, docket_entry):
    with transaction.atomic():
        Docket.objects.select_for_update().get(pk=d.pk)
        pacer_seq_no = docket_entry.get("pacer_seq_no")
        params = {
            "docket": d,
            "entry_number": docket_entry["document_number"],
        }
        if pacer_seq_no is not None:
            params["pacer_sequence_number"] = pacer_seq_no
        null_de_queryset = DocketEntry.objects.filter(
            docket=d,
            entry_number=docket_entry["document_number"],
            pacer_sequence_number__isnull=True,
        )
        try:
            de = DocketEntry.objects.get(**params)
            de_created = False
        except DocketEntry.DoesNotExist:
            if pacer_seq_no is not None and null_de_queryset.exists():
                de = null_de_queryset.latest("date_created")
                null_de_queryset.exclude(pk=de.pk).delete()
                de_created = False
            else:
                de = DocketEntry.objects.create(**params)
                de_created = True
        except DocketEntry.MultipleObjectsReturned:
            if pacer_seq_no is None:
                logger.error(
                    "Multiple docket entries found for document "
                    "entry number '%s' while processing '%s'",
                    docket_entry["document_number"],
                    d,
                )
                return None

            try:
                de = DocketEntry.objects.get(
                    docket=d,
                    entry_number=docket_entry["document_number"],
                    pacer_sequence_number=pacer_seq_no,
                )
                de_created = False
                null_de_queryset.delete()
            except DocketEntry.DoesNotExist:
                if null_de_queryset.exists():
                    de = null_de_queryset.latest("date_created")
                    null_de_queryset.exclude(pk=de.pk).delete()
                    de_created = False
                else:
                    de = DocketEntry.objects.create(
                        docket=d,
                        entry_number=docket_entry["document_number"],
                        pacer_sequence_number=pacer_seq_no,
                    )
                    de_created = True
            except DocketEntry.MultipleObjectsReturned:
                duplicate_de_queryset = DocketEntry.objects.filter(
                    docket=d,
                    entry_number=docket_entry["document_number"],
                    pacer_sequence_number=pacer_seq_no,
                )
                de = duplicate_de_queryset.latest("date_created")
                duplicate_de_queryset.exclude(pk=de.pk).delete()
                null_de_queryset.delete()
                de_created = False

        return de, de_created


async def get_or_make_docket_entry(
    d: Docket, docket_entry: dict[str, any]
) -> tuple[DocketEntry, bool] | None:
    """Lookup or create a docket entry to match the one that was scraped.

    :param d: The docket we expect to find it in.
    :param docket_entry: The scraped dict from Juriscraper for the docket
    entry.
    :return Tuple of (de, de_created) or None, where:
     - de is the DocketEntry object
     - de_created is a boolean stating whether de was created or not
     - None is returned when things fail.
    """
    if docket_entry["document_number"]:
        response = await add_create_docket_entry_transaction(d, docket_entry)
        if response is None:
            return None
        de, de_created = response[0], response[1]
    else:
        # Unnumbered entry. The only thing we can be sure we have is a
        # date. Try to find it by date and description (short or long)
        normalize_long_description(docket_entry)
        query = Q()
        if docket_entry.get("description"):
            query |= Q(description=docket_entry["description"])
        if docket_entry.get("short_description"):
            query |= Q(
                recap_documents__description=docket_entry["short_description"]
            )

        des = DocketEntry.objects.filter(
            query,
            docket=d,
            date_filed=docket_entry["date_filed"],
            entry_number=docket_entry["document_number"],
        )
        count = await des.acount()
        if count == 0:
            de = DocketEntry(
                docket=d, entry_number=docket_entry["document_number"]
            )
            de_created = True
        elif count == 1:
            de = await des.afirst()
            de_created = False
        else:
            logger.warning(
                "Multiple docket entries returned for unnumbered docket "
                "entry on date: %s while processing %s. Attempting merge",
                docket_entry["date_filed"],
                d,
            )
            # There's so little metadata with unnumbered des that if there's
            # more than one match, we can just select the oldest as canonical.
            de = await merge_unnumbered_docket_entries(des, docket_entry)
            de_created = False
    return de, de_created


async def keep_latest_rd_document(queryset: QuerySet) -> RECAPDocument:
    """Retains the most recent item with a PDF, if available otherwise,
    retains the most recent item overall.

    :param queryset: RECAPDocument QuerySet to clean duplicates from.
    :return: The matched RECAPDocument after cleaning.
    """
    rd_with_pdf_queryset = queryset.filter(is_available=True).exclude(
        filepath_local=""
    )
    if await rd_with_pdf_queryset.aexists():
        rd = await rd_with_pdf_queryset.alatest("date_created")
    else:
        rd = await queryset.alatest("date_created")
    await queryset.exclude(pk=rd.pk).adelete()
    return rd


async def clean_duplicate_documents(params: dict[str, Any]) -> RECAPDocument:
    """Removes duplicate RECAPDocuments, keeping the most recent with PDF if
    available or otherwise the most recent overall.

    :param params: Query parameters to filter the RECAPDocuments.
    :return: The matched RECAPDocument after cleaning.
    """
    duplicate_rd_queryset = RECAPDocument.objects.filter(**params)
    return await keep_latest_rd_document(duplicate_rd_queryset)


async def add_docket_entries(
    d: Docket,
    docket_entries: list[dict[str, Any]],
    tags: list[Tag] | None = None,
    do_not_update_existing: bool = False,
) -> tuple[
    tuple[list[DocketEntry], list[RECAPDocument]], list[RECAPDocument], bool
]:
    """Update or create the docket entries and documents.

    :param d: The docket object to add things to and use for lookups.
    :param docket_entries: A list of dicts containing docket entry data.
    :param tags: A list of tag objects to apply to the recap documents and
    docket entries created or updated in this function.
    :param do_not_update_existing: Whether docket entries should only be created and avoid
    updating an existing one.
    :return: A three tuple of:
        - A two tuple of list of created or existing DocketEntry objects and
        a list of existing RECAPDocument objects.
        - A list of RECAPDocument objects created.
        - A bool indicating whether any docket entry was created.
    """
    # Remove items without a date filed value.
    docket_entries = [de for de in docket_entries if de.get("date_filed")]

    rds_created = []
    des_returned = []
    rds_updated = []
    content_updated = False
    calculate_recap_sequence_numbers(docket_entries, d.court_id)
    known_filing_dates = [d.date_last_filing]
    for docket_entry in docket_entries:
        response = await get_or_make_docket_entry(d, docket_entry)
        if response is None:
            continue
        else:
            de, de_created = response[0], response[1]

        de.description = docket_entry["description"] or de.description
        date_filed, time_filed = localize_date_and_time(
            d.court_id, docket_entry["date_filed"]
        )
        if not time_filed:
            # If not time data is available, compare if date_filed changed if
            # so restart time_filed to None, otherwise keep the current time.
            if de.date_filed != docket_entry["date_filed"]:
                de.time_filed = None
        else:
            de.time_filed = time_filed
        de.date_filed = date_filed
        de.pacer_sequence_number = (
            docket_entry.get("pacer_seq_no") or de.pacer_sequence_number
        )
        de.recap_sequence_number = docket_entry["recap_sequence_number"]
        des_returned.append(de)
        if do_not_update_existing and not de_created:
            return (des_returned, rds_updated), rds_created, content_updated
        await de.asave()
        if tags:
            for tag in tags:
                await sync_to_async(tag.tag_object)(de)

        if de_created:
            content_updated = True
            known_filing_dates.append(de.date_filed)

        # Then make the RECAPDocument object. Try to find it. If we do, update
        # the pacer_doc_id field if it's blank. If we can't find it, create it
        # or throw an error.
        params = {"docket_entry": de}
        if not docket_entry["document_number"] and docket_entry.get(
            "short_description"
        ):
            params["description"] = docket_entry["short_description"]

        if docket_entry.get("attachment_number"):
            params["document_type"] = RECAPDocument.ATTACHMENT
            params["attachment_number"] = docket_entry["attachment_number"]
        else:
            params["document_type"] = RECAPDocument.PACER_DOCUMENT

        # Unlike district and bankr. dockets, where you always have a main
        # RD and can optionally have attachments to the main RD, Appellate
        # docket entries can either they *only* have a main RD (with no
        # attachments) or they *only* have attachments (with no main doc).
        # Unfortunately, when we ingest a docket, we don't know if the entries
        # have attachments, so we begin by assuming they don't and create
        # main RDs for each entry. Later, if/when we get attachment pages for
        # particular entries, we convert the main documents into attachment
        # RDs. The check here ensures that if that happens for a particular
        # entry, we avoid creating the main RD a second+ time when we get the
        # docket sheet a second+ time.

        appellate_court_id_exists = await ais_appellate_court(d.court_id)
        appellate_rd_att_exists = False
        if de_created is False and appellate_court_id_exists:
            # In existing appellate entry merges, check if the entry has at
            # least one attachment.
            appellate_rd_att_exists = await de.recap_documents.filter(
                document_type=RECAPDocument.ATTACHMENT
            ).aexists()
            if appellate_rd_att_exists:
                params["document_type"] = RECAPDocument.ATTACHMENT
                params["pacer_doc_id"] = docket_entry["pacer_doc_id"]
        try:
            get_params = deepcopy(params)
            if de_created is False and not appellate_court_id_exists:
                get_params["pacer_doc_id"] = docket_entry["pacer_doc_id"]
            if de_created is False:
                # Try to match the RD regardless of the document_type.
                del get_params["document_type"]
            rd = await RECAPDocument.objects.aget(**get_params)
            rds_updated.append(rd)
        except RECAPDocument.DoesNotExist:
            rd = None
            if de_created is False and not appellate_court_id_exists:
                try:
                    # Check for documents with a bad pacer_doc_id
                    rd = await RECAPDocument.objects.aget(**params)
                except RECAPDocument.DoesNotExist:
                    # Fallback to creating document
                    pass
                except RECAPDocument.MultipleObjectsReturned:
                    rd = await clean_duplicate_documents(params)
            if rd is None:
                try:
                    params["pacer_doc_id"] = docket_entry["pacer_doc_id"]
                    rd = await RECAPDocument.objects.acreate(
                        document_number=docket_entry["document_number"] or "",
                        is_available=False,
                        **params,
                    )
                    rds_created.append(rd)
                except ValidationError:
                    # Happens from race conditions.
                    continue
        except RECAPDocument.MultipleObjectsReturned:
            logger.info(
                "Multiple recap documents found for document entry number'%s' "
                "while processing '%s'",
                docket_entry["document_number"],
                d,
            )
            if params["document_type"] == RECAPDocument.ATTACHMENT:
                continue
            rd = await clean_duplicate_documents(params)

        if docket_entry["pacer_doc_id"]:
            rd.pacer_doc_id = docket_entry["pacer_doc_id"]
        description = docket_entry.get("short_description")
        if rd.document_type == RECAPDocument.PACER_DOCUMENT and description:
            rd.description = description
        elif description:
            rd_qs = de.recap_documents.filter(
                document_type=RECAPDocument.PACER_DOCUMENT
            )
            if await rd_qs.aexists():
                rd_pd = await rd_qs.afirst()
                if rd_pd.attachment_number is not None:
                    continue
                if rd_pd.description != description:
                    rd_pd.description = description
                    try:
                        await rd_pd.asave()
                    except ValidationError:
                        # Happens from race conditions.
                        continue
        rd.document_number = docket_entry["document_number"] or ""
        try:
            await rd.asave()
        except ValidationError:
            # Happens from race conditions.
            continue
        if tags:
            for tag in tags:
                await sync_to_async(tag.tag_object)(rd)

        attachments = docket_entry.get("attachments")
        if attachments is not None:
            court = await Court.objects.aget(pk=d.court_id)
            await merge_attachment_page_data(
                court,
                d.pacer_case_id,
                rd.pacer_doc_id,
                docket_entry["document_number"],
                None,
                attachments,
                False,
            )

    known_filing_dates = set(filter(None, known_filing_dates))
    if known_filing_dates:
        await Docket.objects.filter(pk=d.pk).aupdate(
            date_last_filing=max(known_filing_dates)
        )

    return (des_returned, rds_updated), rds_created, content_updated


def check_json_for_terminated_entities(parties) -> bool:
    """Check the parties and attorneys to find if any terminated entities

    If so, we can assume that the user checked the box for "Terminated Parties"
    before running their docket report. If not, we can assume they didn't.

    :param parties: List of party dicts, as returned by Juriscraper.
    :returns boolean indicating whether any parties had termination dates.
    """
    for party in parties:
        if party.get("date_terminated"):
            return True
        for atty in party.get("attorneys", []):
            terminated_role = {a["role"] for a in atty["roles"]} & {
                Role.TERMINATED,
                Role.SELF_TERMINATED,
            }
            if terminated_role:
                return True
    return False


def get_terminated_entities(d):
    """Check the docket to identify if there were any terminated parties or
    attorneys. If so, return their IDs.

    :param d: A docket object to investigate.
    :returns (parties, attorneys): A tuple of two sets. One for party IDs, one
    for attorney IDs.
    """
    # This will do five queries at most rather than doing potentially hundreds
    # on cases with many parties.
    parties = (
        d.parties.prefetch_related(
            Prefetch(
                "party_types",
                queryset=PartyType.objects.filter(docket=d)
                .exclude(date_terminated=None)
                .distinct()
                .only("pk"),
                to_attr="party_types_for_d",
            ),
            Prefetch(
                "attorneys",
                queryset=Attorney.objects.filter(roles__docket=d)
                .distinct()
                .only("pk"),
                to_attr="attys_in_d",
            ),
            Prefetch(
                "attys_in_d__roles",
                queryset=Role.objects.filter(
                    docket=d, role__in=[Role.SELF_TERMINATED, Role.TERMINATED]
                )
                .distinct()
                .only("pk"),
                to_attr="roles_for_atty",
            ),
        )
        .distinct()
        .only("pk")
    )
    terminated_party_ids = set()
    terminated_attorney_ids = set()
    for party in parties:
        for _ in party.party_types_for_d:
            # PartyTypes are filtered to terminated objects. Thus, if
            # any exist, we know it's a terminated party.
            terminated_party_ids.add(party.pk)
            break
        for atty in party.attys_in_d:
            for _ in atty.roles_for_atty:
                # Roles are filtered to terminated roles. Thus, if any hits, we
                # know we have terminated attys.
                terminated_attorney_ids.add(atty.pk)
                break
    return terminated_party_ids, terminated_attorney_ids


def normalize_attorney_roles(parties):
    """Clean up the attorney roles for all parties.

    We do this fairly early in the process because we need to know if
    there are any terminated attorneys before we can start
    adding/removing content to/from the database. By normalizing
    early, we ensure we have good data for that sniffing.

    A party might be input with an attorney such as:

        {
            'name': 'William H. Narwold',
            'contact': ("1 Corporate Center\n",
                        "20 Church Street\n",
                        "17th Floor\n",
                        "Hartford, CT 06103\n",
                        "860-882-1676\n",
                        "Fax: 860-882-1682\n",
                        "Email: bnarwold@motleyrice.com"),
            'roles': ['LEAD ATTORNEY',
                      'TERMINATED: 03/12/2013'],
        }

    The role attribute will be cleaned up to be:

        'roles': [{
            'role': Role.ATTORNEY_LEAD,
            'date_action': None,
            'role_raw': 'LEAD ATTORNEY',
        }, {
            'role': Role.TERMINATED,
            'date_action': date(2013, 3, 12),
            'role_raw': 'TERMINATED: 03/12/2013',
        }

    :param parties: The parties dict from Juriscraper.
    :returns None; editing happens in place.

    """
    for party in parties:
        for atty in party.get("attorneys", []):
            roles = [normalize_attorney_role(r) for r in atty["roles"]]
            roles = remove_duplicate_dicts(roles)
            atty["roles"] = roles


def disassociate_extraneous_entities(
    d, parties, parties_to_preserve, attorneys_to_preserve
):
    """Disassociate any parties or attorneys no longer in the latest info.

     - Do not delete parties or attorneys, just allow them to be orphaned.
       Later, we can decide what to do with these, but keeping them around at
       least lets us have them later if we need them.

     - Sort out if terminated parties were included in the new data. If so,
       they'll be automatically preserved (because they would have been
       updated). If not, find the old terminated parties on the docket, and
       prevent them from getting disassociated.

     - If a party is terminated, do not delete their attorneys even if their
       attorneys are not listed as terminated.

    :param d: The docket to interrogate and act upon.
    :param parties: The parties dict that was scraped, and which we inspect to
    check if terminated parties were included.
    :param parties_to_preserve: A set of party IDs that were updated or created
    while updating the docket.
    :param attorneys_to_preserve: A set of attorney IDs that were updated or
    created while updating the docket.
    """
    new_has_terminated_entities = check_json_for_terminated_entities(parties)
    if not new_has_terminated_entities:
        # No terminated data in the JSON. Check if we have any in the DB.
        terminated_parties, terminated_attorneys = get_terminated_entities(d)
        if any([terminated_parties, terminated_attorneys]):
            # The docket currently has terminated entities, but new info
            # doesn't, indicating that the user didn't request it. Thus, delete
            # any entities that weren't just created/updated and that aren't in
            # the list of terminated entities.
            parties_to_preserve = parties_to_preserve | terminated_parties
            attorneys_to_preserve = (
                attorneys_to_preserve | terminated_attorneys
            )
    else:
        # The terminated parties are already included in the entities to
        # preserve, so just create an empty variable for this.
        terminated_parties = set()

    # Disassociate extraneous parties from the docket.
    PartyType.objects.filter(
        docket=d,
    ).exclude(
        party_id__in=parties_to_preserve,
    ).delete()

    # Disassociate extraneous attorneys from the docket and parties.
    Role.objects.filter(
        docket=d,
    ).exclude(
        # Don't delete attorney roles for attorneys we're preserving.
        attorney_id__in=attorneys_to_preserve,
    ).exclude(
        # Don't delete attorney roles for parties we're preserving b/c
        # they were terminated.
        party_id__in=terminated_parties,
    ).delete()


@transaction.atomic
# Retry on transaction deadlocks; see #814.
@retry(OperationalError, tries=2, delay=1, backoff=1, logger=logger)
def add_parties_and_attorneys(d, parties):
    """Add parties and attorneys from the docket data to the docket.

    :param d: The docket to update
    :param parties: The parties to update the docket with, with their
    associated attorney objects. This is typically the
    docket_data['parties'] field.
    :return: None

    """
    if not parties:
        # Exit early if no parties. Some dockets don't have any due to user
        # preference, and if we don't bail early, we risk deleting everything
        # we have.
        return

    # Recall that Python is pass by reference. This means that if we mutate
    # the parties variable in this function and then retry this function (note
    # the decorator it has), the second time this function runs, it will not be
    # run with the initial value of the parties variable, but will instead be
    # run with the mutated value! That will crash because the mutated variable
    # no longer has the correct shape as it did when it was first passed.
    # ∴, make a copy of parties as a first step, so that retries work.
    local_parties = deepcopy(parties)

    normalize_attorney_roles(local_parties)

    updated_parties = set()
    updated_attorneys = set()
    for party in local_parties:
        ps = Party.objects.filter(
            name=party["name"], party_types__docket=d
        ).distinct()
        count = ps.count()
        if count == 0:
            try:
                p = Party.objects.create(name=party["name"])
            except IntegrityError:
                # Race condition. Object was created after our get and before
                # our create. Try to get it again.
                ps = Party.objects.filter(
                    name=party["name"], party_types__docket=d
                ).distinct()
                count = ps.count()
        if count == 1:
            p = ps[0]
        elif count >= 2:
            p = ps.earliest("date_created")
        updated_parties.add(p.pk)

        # If the party type doesn't exist, make a new one.
        pts = p.party_types.filter(docket=d, name=party["type"])
        criminal_data = party.get("criminal_data")
        update_dict = {
            "extra_info": party.get("extra_info", ""),
            "date_terminated": party.get("date_terminated"),
        }
        if criminal_data:
            update_dict["highest_offense_level_opening"] = criminal_data[
                "highest_offense_level_opening"
            ]
            update_dict["highest_offense_level_terminated"] = criminal_data[
                "highest_offense_level_terminated"
            ]
        if pts.exists():
            pts.update(**update_dict)
            pt = pts[0]
        else:
            pt = PartyType.objects.create(
                docket=d, party=p, name=party["type"], **update_dict
            )

        # Criminal counts and complaints
        if criminal_data and criminal_data["counts"]:
            CriminalCount.objects.filter(party_type=pt).delete()
            CriminalCount.objects.bulk_create(
                [
                    CriminalCount(
                        party_type=pt,
                        name=criminal_count["name"],
                        disposition=criminal_count["disposition"],
                        status=CriminalCount.normalize_status(
                            criminal_count["status"]
                        ),
                    )
                    for criminal_count in criminal_data["counts"]
                ]
            )

        if criminal_data and criminal_data["complaints"]:
            CriminalComplaint.objects.filter(party_type=pt).delete()
            CriminalComplaint.objects.bulk_create(
                [
                    CriminalComplaint(
                        party_type=pt,
                        name=complaint["name"],
                        disposition=complaint["disposition"],
                    )
                    for complaint in criminal_data["complaints"]
                ]
            )

        # Attorneys
        for atty in party.get("attorneys", []):
            updated_attorneys.add(add_attorney(atty, p, d))

    disassociate_extraneous_entities(
        d, local_parties, updated_parties, updated_attorneys
    )


@transaction.atomic
def add_bankruptcy_data_to_docket(d: Docket, metadata: dict[str, str]) -> None:
    """Add bankruptcy data to the docket from the claims data, RSS feeds, or
    another location.
    """
    try:
        bankr_data = d.bankruptcy_information
    except BankruptcyInformation.DoesNotExist:
        bankr_data = BankruptcyInformation(docket=d)

    do_save = False
    for field in bankruptcy_data_fields:
        if metadata.get(field):
            do_save = True
            setattr(bankr_data, field, metadata[field])

    if do_save:
        bankr_data.save()


def add_claim_history_entry(new_history, claim):
    """Add a document from a claim's history table to the database.

    These documents can reference docket entries or documents that only exist
    in the claims registry. Whatever the case, we just make an entry in the
    claims history table. For now we don't try to link the docket entry table
    with the claims table. It's doable, but adds complexity.

    Further, we also don't handle unnumbered claims. You can see an example of
    one of these in Juriscraper in the txeb.html example file. Here, we just
    punt on these.

    :param new_history: The history dict returned by juriscraper.
    :param claim: The claim in the database the history is associated with.
    :return None
    """
    if new_history["document_number"] is None:
        # Punt on unnumbered claims.
        return

    history_type = new_history["type"]
    common_lookup_params = {
        "claim": claim,
        "date_filed": new_history["date_filed"],
        # Sometimes missing when a docket entry type
        # doesn't have a link for some reason.
        "pacer_case_id": new_history.get("pacer_case_id", ""),
        "document_number": new_history["document_number"],
    }

    if history_type == "docket_entry":
        db_history, _ = ClaimHistory.objects.get_or_create(
            claim_document_type=ClaimHistory.DOCKET_ENTRY,
            pacer_doc_id=new_history.get("pacer_doc_id", ""),
            **common_lookup_params,
        )
        db_history.pacer_dm_id = (
            new_history.get("pacer_dm_id") or db_history.pacer_dm_id
        )
        db_history.pacer_seq_no = new_history.get("pacer_seq_no")

    else:
        db_history, _ = ClaimHistory.objects.get_or_create(
            claim_document_type=ClaimHistory.CLAIM_ENTRY,
            claim_doc_id=new_history["id"],
            attachment_number=new_history["attachment_number"],
            **common_lookup_params,
        )

    db_history.description = (
        new_history.get("description") or db_history.description
    )
    db_history.save()


@transaction.atomic
def add_claims_to_docket(d, new_claims, tag_names=None):
    """Add claims data to the docket.

    :param d: A docket object to associate claims with.
    :param new_claims: A list of claims dicts from Juriscraper.
    :param tag_names: A list of tag names to add to the claims.
    """
    for new_claim in new_claims:
        db_claim, _ = Claim.objects.get_or_create(
            docket=d, claim_number=new_claim["claim_number"]
        )
        db_claim.date_claim_modified = (
            new_claim.get("date_claim_modified")
            or db_claim.date_claim_modified
        )
        db_claim.date_original_entered = (
            new_claim.get("date_original_entered")
            or db_claim.date_original_entered
        )
        db_claim.date_original_filed = (
            new_claim.get("date_original_filed")
            or db_claim.date_original_filed
        )
        db_claim.date_last_amendment_entered = (
            new_claim.get("date_last_amendment_entered")
            or db_claim.date_last_amendment_entered
        )
        db_claim.date_last_amendment_filed = (
            new_claim.get("date_last_amendment_filed")
            or db_claim.date_last_amendment_filed
        )
        db_claim.creditor_details = (
            new_claim.get("creditor_details") or db_claim.creditor_details
        )
        db_claim.creditor_id = (
            new_claim.get("creditor_id") or db_claim.creditor_id
        )
        db_claim.status = new_claim.get("status") or db_claim.status
        db_claim.entered_by = (
            new_claim.get("entered_by") or db_claim.entered_by
        )
        db_claim.filed_by = new_claim.get("filed_by") or db_claim.filed_by
        db_claim.amount_claimed = (
            new_claim.get("amount_claimed") or db_claim.amount_claimed
        )
        db_claim.unsecured_claimed = (
            new_claim.get("unsecured_claimed") or db_claim.unsecured_claimed
        )
        db_claim.secured_claimed = (
            new_claim.get("secured_claimed") or db_claim.secured_claimed
        )
        db_claim.priority_claimed = (
            new_claim.get("priority_claimed") or db_claim.priority_claimed
        )
        db_claim.description = (
            new_claim.get("description") or db_claim.description
        )
        db_claim.remarks = new_claim.get("remarks") or db_claim.remarks
        db_claim.save()
        async_to_sync(add_tags_to_objs)(tag_names, [db_claim])
        for new_history in new_claim["history"]:
            add_claim_history_entry(new_history, db_claim)


def get_data_from_att_report(text: str, court_id: str) -> dict[str, str]:
    att_page = AttachmentPage(map_cl_to_pacer_id(court_id))
    att_page._parse_text(text)
    att_data = att_page.data
    return att_data


def get_data_from_appellate_att_report(
    text: str, court_id: str
) -> dict[str, str]:
    """Get attachments data from Juriscraper AppellateAttachmentPage

    :param text: The attachment page text to parse.
    :param court_id: The CourtListener court_id we're working with
    :return: The appellate attachment page data
    """
    att_page = AppellateAttachmentPage(map_cl_to_pacer_id(court_id))
    att_page._parse_text(text)
    att_data = att_page.data
    return att_data


async def add_tags_to_objs(tag_names: list[str], objs: Any) -> list[Tag]:
    """Add tags by name to objects

    :param tag_names: A list of tag name strings
    :type tag_names: list
    :param objs: A list of objects in need of tags
    :type objs: list
    :return: [] if no tag names, else a list of the tags created/found
    """
    if tag_names is None:
        return []

    tags: list[Tag] = []
    for tag_name in tag_names:
        tag, _ = await Tag.objects.aget_or_create(name=tag_name)
        tags.append(tag)

    for tag in tags:
        for obj in objs:
            await sync_to_async(tag.tag_object)(obj)
    return tags


@transaction.atomic
def merge_pacer_docket_into_cl_docket(
    d, pacer_case_id, docket_data, report, appellate=False, tag_names=None
):
    # Ensure that we set the case ID. This is needed on dockets that have
    # matching docket numbers, but that never got PACER data before. This was
    # previously rare, but since we added the FJC data to the dockets table,
    # this is now quite common.
    if not d.pacer_case_id:
        d.pacer_case_id = pacer_case_id

    d.add_recap_source()
    async_to_sync(update_docket_metadata)(d, docket_data)

    # Skip the percolator request for this save if parties data will be merged
    # afterward.
    set_skip_percolation_if_parties_data(docket_data["parties"], d)
    d.save()

    if appellate:
        d, og_info = async_to_sync(update_docket_appellate_metadata)(
            d, docket_data
        )
        if og_info is not None:
            og_info.save()
            d.originating_court_information = og_info

    tags = async_to_sync(add_tags_to_objs)(tag_names, [d])

    # Add the HTML to the docket in case we need it someday.
    upload_type = (
        UPLOAD_TYPE.APPELLATE_DOCKET if appellate else UPLOAD_TYPE.DOCKET
    )
    pacer_file = PacerHtmlFiles(content_object=d, upload_type=upload_type)

    # Determine how to store the report data.
    # Most PACER reports include a raw HTML response and set the `response`
    # attribute. However, ACMS reports typically construct the data from a
    # series of API calls, and do not include a single HTML response. In those
    # cases, we store the data as JSON instead.
    pacer_file_name = "docket.html" if report.response else "docket.json"
    pacer_file_content = (
        report.response.text.encode()
        if report.response
        else json.dumps(report.data, default=str).encode()
    )
    pacer_file.filepath.save(
        pacer_file_name,  # We only care about the ext w/S3PrivateUUIDStorageTest
        ContentFile(pacer_file_content),
    )

    # Merge parties before adding docket entries, so they can access parties'
    # data when the RECAPDocuments are percolated.
    add_parties_and_attorneys(d, docket_data["parties"])
    if docket_data["parties"]:
        # Index or re-index parties only if the docket has parties.
        index_docket_parties_in_es.delay(d.pk)

    items_returned, rds_created, content_updated = async_to_sync(
        add_docket_entries
    )(d, docket_data["docket_entries"], tags=tags)
    async_to_sync(process_orphan_documents)(
        rds_created, d.court_id, d.date_filed
    )
    logger.info("Created/updated docket: %s", d)
    return rds_created, content_updated


async def clean_duplicate_attachment_entries(
    de: DocketEntry,
    attachment_dicts: list[dict[str, int | str]],
):
    """Remove attachment page entries with duplicate pacer_doc_id's that
    have incorrect attachment numbers. This is needed because older attachment
    pages were incorrectly parsed. See: freelawproject/juriscraper#721

    Also generically remove attachments with duplicate pacer_doc_id's which
    may have been duplicated due to issues with document_number parsing.

    :param de: A DocketEntry object
    :param attachment_dicts: A list of Juriscraper-parsed dicts for each
    attachment.
    """
    rds = RECAPDocument.objects.filter(docket_entry=de)

    dupe_doc_ids = (
        rds.values("pacer_doc_id")
        .annotate(Count("id"))
        .order_by()
        .filter(id__count__gt=1)
    )

    if not await dupe_doc_ids.aexists():
        return
    dupes = rds.filter(
        pacer_doc_id__in=[
            i["pacer_doc_id"] async for i in dupe_doc_ids.aiterator()
        ]
    )
    async for dupe in dupes.aiterator():
        for attachment in attachment_dicts:
            attachment_number = attachment["attachment_number"]
            pacer_doc_id = attachment["pacer_doc_id"]
            if dupe.pacer_doc_id == pacer_doc_id:
                if dupe.attachment_number != attachment_number:
                    await dupe.adelete()
    if not await dupe_doc_ids.aexists():
        return
    dupes = rds.filter(
        pacer_doc_id__in=[
            i["pacer_doc_id"] async for i in dupe_doc_ids.aiterator()
        ]
    )
    async for dupe in dupes.aiterator():
        duplicate_rd_queryset = rds.filter(pacer_doc_id=dupe.pacer_doc_id)
        await keep_latest_rd_document(duplicate_rd_queryset)


async def merge_attachment_page_data(
    court: Court,
    pacer_case_id: int,
    pacer_doc_id: int,
    document_number: int | None,
    text: str | None,
    attachment_dicts: list[dict[str, int | str]],
    debug: bool = False,
    is_acms_attachment: bool = False,
) -> tuple[list[RECAPDocument], DocketEntry]:
    """Merge attachment page data into the docket

    :param court: The court object we're working with
    :param pacer_case_id: A PACER case ID
    :param pacer_doc_id: A PACER document ID
    :param document_number: The docket entry number
    :param text: The text of the attachment page
    :param attachment_dicts: A list of Juriscraper-parsed dicts for each
    attachment.
    :param debug: Whether to do saves during this process.
    :param is_acms_attachment: Whether the attachments come from ACMS.
    :return: A list of RECAPDocuments modified or created during the process,
    and the DocketEntry object associated with the RECAPDocuments
    :raises: RECAPDocument.MultipleObjectsReturned, RECAPDocument.DoesNotExist
    """
    # Create/update the attachment items.
    rds_created = []
    rds_affected = []
    params = {
        "pacer_doc_id": pacer_doc_id,
        "docket_entry__docket__court": court,
    }
    if pacer_case_id:
        params["docket_entry__docket__pacer_case_id"] = pacer_case_id
    try:
        if is_acms_attachment:
            # Recap documents on ACMS attachment pages share the same pacer_case_id
            # which causes an issue when using the aget method. Since the aget
            # method expects a unique identifier to retrieve a specific document,
            # utilizing it in this scenario would inevitably result in the
            # MultipleObjectsReturned exception.
            #
            # An alternative approach is to employ the filter method in conjunction
            # with the afirst method. This combination allows for efficient retrieval
            # of the main RD (record) of a docket entry.
            main_rd = (
                await RECAPDocument.objects.select_related(
                    "docket_entry", "docket_entry__docket"
                )
                .filter(**params)
                .afirst()
            )
        else:
            main_rd = await RECAPDocument.objects.select_related(
                "docket_entry", "docket_entry__docket"
            ).aget(**params)

    except RECAPDocument.MultipleObjectsReturned as exc:
        if pacer_case_id:
            await clean_duplicate_documents(params)
            main_rd = await RECAPDocument.objects.select_related(
                "docket_entry", "docket_entry__docket"
            ).aget(**params)
        else:
            # Unclear how to proceed and we don't want to associate this data
            # with the wrong case. We must punt.
            raise exc
    except RECAPDocument.DoesNotExist as exc:
        found_main_rd = False
        migrated_description = ""
        if not is_acms_attachment:
            for attachment in attachment_dicts:
                if attachment.get("pacer_doc_id", False):
                    params["pacer_doc_id"] = attachment["pacer_doc_id"]
                try:
                    main_rd = await RECAPDocument.objects.select_related(
                        "docket_entry", "docket_entry__docket"
                    ).aget(**params)
                    if attachment.get("attachment_number", 0) != 0:
                        main_rd.attachment_number = attachment[
                            "attachment_number"
                        ]
                        main_rd.document_type = RECAPDocument.ATTACHMENT
                        migrated_description = main_rd.description
                        await main_rd.asave()
                    found_main_rd = True
                    break
                except RECAPDocument.MultipleObjectsReturned as exc:
                    if pacer_case_id:
                        await clean_duplicate_documents(params)
                        main_rd = await RECAPDocument.objects.select_related(
                            "docket_entry", "docket_entry__docket"
                        ).aget(**params)
                        if attachment.get("attachment_number", 0) != 0:
                            main_rd.attachment_number = attachment[
                                "attachment_number"
                            ]
                            main_rd.document_type = RECAPDocument.ATTACHMENT
                            migrated_description = main_rd.description
                            await main_rd.asave()
                        found_main_rd = True
                        break
                    else:
                        # Unclear how to proceed and we don't want to associate
                        # this data with the wrong case. We must punt.
                        raise exc
                except RECAPDocument.DoesNotExist:
                    continue
        # Can't find the docket to associate with the attachment metadata
        # It may be possible to go look for orphaned documents at this stage
        # and to then add them here, as we do when adding dockets. This need is
        # particularly acute for those that get free look emails and then go to
        # the attachment page.
        if not found_main_rd:
            raise exc
        else:
            rd = RECAPDocument(
                docket_entry=main_rd.docket_entry,
                document_type=RECAPDocument.PACER_DOCUMENT,
                document_number=main_rd.document_number,
                description=migrated_description,
                pacer_doc_id=pacer_doc_id,
            )
            rds_created.append(rd)
            rds_affected.append(rd)
            await rd.asave()

    # We got the right item. Update/create all the attachments for
    # the docket entry.
    de = main_rd.docket_entry
    if document_number is None:
        # Bankruptcy or Appellate attachment page. Use the document number from
        # the Main doc
        document_number = main_rd.document_number

    if debug:
        return [], de

    # Save the old HTML to the docket entry.
    # We won't have text if attachments are from docket page.
    if text is not None:
        pacer_file = await sync_to_async(PacerHtmlFiles)(
            content_object=de, upload_type=UPLOAD_TYPE.ATTACHMENT_PAGE
        )
        pacer_file_name = (
            "attachment_page.json"
            if is_acms_attachment
            else "attachment_page.html"
        )
        await sync_to_async(pacer_file.filepath.save)(
            pacer_file_name,  # Irrelevant b/c S3PrivateUUIDStorageTest
            ContentFile(text.encode()),
        )

    court_is_appellate = await ais_appellate_court(court.pk)
    main_rd_to_att = False
    for attachment in attachment_dicts:
        sanity_checks = [
            attachment.get("attachment_number") is not None,
            # Missing on sealed items.
            attachment.get("pacer_doc_id", False),
        ]
        if not all(sanity_checks):
            continue

        # Missing on some restricted docs (see Juriscraper)
        # Attachment 0 may not have page count since it is the main rd.
        if (
            "page_count" in attachment
            and attachment["page_count"] is None
            and attachment["attachment_number"] != 0
        ):
            continue

        # Appellate entries with attachments don't have a main RD, transform it
        # to an attachment. In ACMS attachment pages, all the documents use the
        # same pacer_doc_id, so we need to make sure only one is matched to the
        # main RD, while the remaining ones are created separately.
        if (
            court_is_appellate
            and attachment["pacer_doc_id"] == main_rd.pacer_doc_id
            and not main_rd_to_att
        ):
            main_rd_to_att = True
            main_rd.document_type = RECAPDocument.ATTACHMENT
            main_rd.attachment_number = attachment["attachment_number"]
            if "acms_document_guid" in attachment:
                main_rd.acms_document_guid = attachment["acms_document_guid"]
            rd = main_rd
        else:
            params = {
                "docket_entry": de,
                "document_number": document_number,
            }
            if attachment["attachment_number"] == 0:
                params["document_type"] = RECAPDocument.PACER_DOCUMENT
            else:
                params["attachment_number"] = attachment["attachment_number"]
                params["document_type"] = RECAPDocument.ATTACHMENT
            if "acms_document_guid" in attachment:
                params["acms_document_guid"] = attachment["acms_document_guid"]
            try:
                rd = await RECAPDocument.objects.aget(**params)
            except RECAPDocument.DoesNotExist:
                try:
                    doc_id_params = deepcopy(params)
                    doc_id_params.pop("attachment_number", None)
                    del doc_id_params["document_type"]
                    doc_id_params["pacer_doc_id"] = attachment["pacer_doc_id"]
                    if (
                        court_is_appellate
                        and is_long_appellate_document_number(document_number)
                    ):
                        # If this attachment page belongs to an appellate court
                        # that doesn't use regular numbers, fallback to matching
                        # the RD while omitting the document_number since it was likely scrambled
                        # due to the bug described in:
                        # https://github.com/freelawproject/courtlistener/issues/2877
                        del doc_id_params["document_number"]
                    rd = await RECAPDocument.objects.aget(**doc_id_params)
                    if attachment["attachment_number"] == 0:
                        try:
                            old_main_rd = await RECAPDocument.objects.aget(
                                docket_entry=de,
                                document_type=RECAPDocument.PACER_DOCUMENT,
                            )
                            rd.description = old_main_rd.description
                        except RECAPDocument.DoesNotExist:
                            rd.description = ""
                        except RECAPDocument.MultipleObjectsReturned:
                            rd.description = ""
                            logger.info(
                                "Failed to migrate description for "
                                "%s, multiple source documents found.",
                                attachment["pacer_doc_id"],
                            )
                        rd.attachment_number = None
                        rd.document_type = RECAPDocument.PACER_DOCUMENT
                    else:
                        rd.attachment_number = attachment["attachment_number"]
                        rd.document_type = RECAPDocument.ATTACHMENT
                except RECAPDocument.DoesNotExist:
                    rd = RECAPDocument(**params)
                    if attachment["attachment_number"] == 0:
                        try:
                            old_main_rd = await RECAPDocument.objects.aget(
                                docket_entry=de,
                                document_type=RECAPDocument.PACER_DOCUMENT,
                            )
                            rd.description = old_main_rd.description
                        except RECAPDocument.DoesNotExist:
                            rd.description = ""
                        except RECAPDocument.MultipleObjectsReturned:
                            rd.description = ""
                            logger.info(
                                "Failed to migrate description for %s, multiple source documents found.",
                                attachment["pacer_doc_id"],
                            )
                    rds_created.append(rd)

        rds_affected.append(rd)
        if (
            attachment["description"]
            and rd.document_type == RECAPDocument.ATTACHMENT
        ):
            rd.description = attachment["description"]
        if attachment["pacer_doc_id"]:
            rd.pacer_doc_id = attachment["pacer_doc_id"]

        if court_is_appellate and is_long_appellate_document_number(
            document_number
        ):
            # If this attachment page belongs to an appellate court
            # that doesn't use regular numbers, assign it from the pacer_doc_id
            # to fix possible scrambled document_numbers.
            rd.document_number = pacer_doc_id

        # Only set page_count and file_size if they're blank, in case
        # we got the real value by measuring.
        if rd.page_count is None and attachment.get("page_count", None):
            rd.page_count = attachment["page_count"]
        # If we have file_size_bytes it should have max precision.
        file_size_bytes = attachment.get("file_size_bytes")
        if file_size_bytes is not None:
            rd.file_size = file_size_bytes
        elif rd.file_size is None and attachment.get("file_size_str", None):
            try:
                rd.file_size = convert_size_to_bytes(
                    attachment["file_size_str"]
                )
            except ValueError:
                pass
        await rd.asave()

    if not is_acms_attachment:
        await clean_duplicate_attachment_entries(de, attachment_dicts)
    await mark_ia_upload_needed(de.docket, save_docket=True)
    await process_orphan_documents(
        rds_created, court.pk, main_rd.docket_entry.docket.date_filed
    )
    return rds_affected, de


def save_iquery_to_docket(
    self,
    iquery_data: dict[str, str],
    iquery_text: str,
    d: Docket,
    tag_names: list[str] | None,
    skip_iquery_sweep: bool = False,
) -> int | None:
    """Merge iquery results into a docket

    :param self: The celery task calling this function
    :param iquery_data: The data from a successful iquery response
    :param iquery_text: The HTML text data from a successful iquery response
    :param d: A docket object to work with
    :param tag_names: Tags to add to the items
    :param skip_iquery_sweep: Whether to avoid triggering the iquery sweep
    signal. Useful for ignoring reports added by the probe daemon or the iquery
    sweep itself.
    :return: The pk of the docket if successful. Else, None.
    """
    d = async_to_sync(update_docket_metadata)(d, iquery_data)
    d.skip_iquery_sweep = skip_iquery_sweep
    # Skip the percolator request for this save if bankruptcy data will
    # be merged afterward.
    set_skip_percolation_if_bankruptcy_data(iquery_data, d)
    try:
        d.save()
        add_bankruptcy_data_to_docket(d, iquery_data)
    except IntegrityError as exc:
        msg = "Integrity error while saving iquery response."
        if self.request.retries == self.max_retries:
            logger.warning(msg)
            return
        logger.info("%s Retrying.", msg)
        raise self.retry(exc=exc)

    async_to_sync(add_tags_to_objs)(tag_names, [d])
    logger.info("Created/updated docket: %s", d)

    # Add the CASE_QUERY_PAGE to the docket in case we need it someday.
    pacer_file = PacerHtmlFiles.objects.create(
        content_object=d, upload_type=UPLOAD_TYPE.CASE_QUERY_PAGE
    )
    pacer_file.filepath.save(
        "case_report.html",  # We only care about the ext w/S3PrivateUUIDStorageTest
        ContentFile(iquery_text.encode()),
    )

    return d.pk


async def process_orphan_documents(
    rds_created: list[RECAPDocument],
    court_id: int,
    docket_date: date,
) -> None:
    """After we finish processing a docket upload add any PDFs we already had
    for that docket that were lingering in our processing queue. This addresses
    the issue that arises when somebody (somehow) uploads a PDF without first
    uploading a docket.
    """
    pacer_doc_ids = [rd.pacer_doc_id for rd in rds_created]
    if docket_date:
        # If we get a date from the docket, set the cutoff to 30 days prior for
        # good measure.
        cutoff_date = docket_date - timedelta(days=30)
    else:
        # No date from docket. Limit ourselves to the last 180 days. This will
        # help prevent items with weird errors from plaguing us forever.
        cutoff_date = now() - timedelta(days=180)
    pqs = ProcessingQueue.objects.filter(
        pacer_doc_id__in=pacer_doc_ids,
        court_id=court_id,
        status=PROCESSING_STATUS.FAILED,
        upload_type=UPLOAD_TYPE.PDF,
        debug=False,
        date_modified__gt=cutoff_date,
    ).values_list("pk", flat=True)
    async for pq in pqs.aiterator():
        try:
            from cl.recap.tasks import process_recap_pdf

            await process_recap_pdf(pq)
        except:
            # We can ignore this. If we don't, we get all of the
            # exceptions that were previously raised for the
            # processing queue items a second time.
            pass


@retry(IntegrityError, tries=3, delay=0.25, backoff=1)
def process_case_query_report(
    court_id: str,
    pacer_case_id: int,
    report_data: dict[str, Any],
    report_text: str,
    skip_iquery_sweep: bool = False,
) -> None:
    """Process the case query report from probe_or_scrape_iquery_pages task.
    Find and update/store the docket accordingly. This method is able to retry
    on IntegrityError due to a race condition when saving the docket.

    :param court_id:  A CL court ID where we'll look things up.
    :param pacer_case_id: The internal PACER case ID number
    :param report_data: A dictionary containing report data.
    :param report_text: The HTML text data from a successful iquery response
    :param skip_iquery_sweep:  Whether to avoid triggering the iquery sweep
    signal. Useful for ignoring reports added by the probe daemon or the iquery
    sweep itself.
    :return: None
    """
    d = async_to_sync(find_docket_object)(
        court_id,
        str(pacer_case_id),
        report_data["docket_number"],
        report_data.get("federal_defendant_number"),
        report_data.get("federal_dn_judge_initials_assigned"),
        report_data.get("federal_dn_judge_initials_referred"),
        using="default",
    )
    d.pacer_case_id = pacer_case_id
    d.add_recap_source()
    d = async_to_sync(update_docket_metadata)(d, report_data)
    d.skip_iquery_sweep = skip_iquery_sweep
    # Skip the percolator request for this save if bankruptcy data will
    # be merged afterward.
    set_skip_percolation_if_bankruptcy_data(report_data, d)
    d.save()
    add_bankruptcy_data_to_docket(d, report_data)
    logger.info(
        "Created/updated docket: %s from court: %s and pacer_case_id %s",
        d,
        court_id,
        pacer_case_id,
    )

    # Add the CASE_QUERY_PAGE to the docket in case we need it someday.
    pacer_file = PacerHtmlFiles.objects.create(
        content_object=d, upload_type=UPLOAD_TYPE.CASE_QUERY_PAGE
    )
    pacer_file.filepath.save(
        "case_report.html",
        # We only care about the ext w/S3PrivateUUIDStorageTest
        ContentFile(report_text.encode()),
    )
    return None
