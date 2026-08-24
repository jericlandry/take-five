import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional

from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from psycopg2.pool import ThreadedConnectionPool

load_dotenv()

# --- Topic analysis constants (used by get_circle_topics) ---

TOPIC_CATEGORIES: Dict[str, list] = {
    'Medical & health': [
        'appointment', 'appt', 'doctor', 'dr.', 'dr ', 'nurse', 'hospital',
        'diagnosis', 'dementia', 'memory', 'cognitive', 'hearing', 'audiologist',
        'sleep', 'anxiety', 'blood pressure', 'weight', 'macular', 'injection',
        'test', 'labs', 'mri', 'decline', 'assisted living', 'memory care',
        'physical therapy', 'therapist', 'psychiatrist', 'geriatric',
        'swallowing', 'fall', 'unsteady', 'wheelchair', 'walker',
    ],
    'Medications': [
        'medication', 'med ', 'meds', 'pill', 'pills', 'prescription',
        'dose', 'dosage', 'tablet', 'capsule', 'supplement', 'vitamin',
        'melatonin', 'thyroid', 'temazepam', 'dayvigo', 'mirabegron',
        'sertraline', 'paroxetine', 'atenolol', 'metoprolol', 'mirtazapine',
        'pharmacy', 'refill', 'pill box', 'med tray', 'biofreeze', 'tylenol',
        'side effect', 'taper',
    ],
    'Life & engagement': [
        'book', 'reading', 'novel', 'james patterson', 'grisham', 'sparks',
        'movie', 'netflix', 'tv show', 'watching',
        'genealogy', 'family history', 'research',
        'walk', 'exercise', 'pickleball', 'bingo', 'poker', 'cards',
        'lunch', 'dinner', 'breakfast', 'restaurant', 'kolache', 'pie',
        'shopping', 'party', 'event', 'happy hour', 'rosary', 'mass', 'church',
        'good spirits', 'good day', 'enjoyed', 'laughed', 'excited', 'proud',
        'mood', 'energy', 'smile',
    ],
    'Logistics & coordination': [
        'visit', 'going down', 'drive', 'driving', 'trip', 'travel',
        'schedule', 'calendar', 'tuesday', 'wednesday', 'thursday', 'friday',
        'monday', 'weekend', 'next week', 'this week',
        'lucy', 'caretaker', 'aide', 'caregiver',
        'family meeting', 'meeting', 'plan', 'coordinate',
        'who is going', 'can you go', 'are you going',
        'eden', 'apartment', 'new braunfels',
    ],
    'Home & tech': [
        'netflix', 'tv', 'television', 'remote', 'spectrum', 'wifi',
        'internet', 'password', 'computer', 'phone', 'claude', 'ai',
        'amazon prime', 'streaming', 'channel', 'router',
        'mattress', 'bed', 'sheets', 'clock', 'hearing aid', 'oticon', 'phonak',
    ],
}

TOPIC_STOPWORDS: frozenset = frozenset({
    'the','and','for','that','this','with','have','from','they','will',
    'been','were','she','her','his','him','our','out','but','not','are',
    'was','had','can','get','got','did','all','just','also','about','when',
    'what','who','how','would','could','should','there','their','them',
    'said','told','told','some','into','than','then','its','mom','dad',
    'meme','poppy','eric','keith','autumn','monica','lee','anne','john',
    'mary','ellen','well','still','know','think','back','want','need',
    'going','went','took','come','came','told','make','like','feel','good',
    'sure','time','day','week','one','two','let','ask','put','try','use',
    'now','new','has','him','her','too','more','very','much','next','last',
    'may','few','any','see','way','hey','yes','yep','nope','haha','lol',
})


class TakeFiveRepository:
    def __init__(self):
        self.db_config = {
            'dbname':   'takefive',
            'user':     os.getenv('DB_USER'),
            'password': os.getenv('DB_PASSWORD'),
            'host':     'dpg-d78po2h5pdvs73b7l7rg-a.virginia-postgres.render.com',
            'port':     5432
        }
        # Connection pool — previously every _execute() call (and every
        # save_clinical_record/patch_clinical_record/invite_person_to_ensemble
        # transaction) opened a brand-new psycopg2.connect() from scratch,
        # paying a full TCP+SSL+auth handshake every single time. Measured at
        # ~0.6s/query on Render's hosted Postgres from a warm process — a
        # single ask_with_tools() question makes 6 of these back to back, all
        # paying that cost with nothing to show for it since none of it is
        # query execution time.
        #
        # ThreadedConnectionPool (not SimpleConnectionPool): blocking DB
        # calls are the planned next asyncio.to_thread() candidate, which
        # would put concurrent calls on real OS threads — SimpleConnectionPool
        # is not safe for that, ThreadedConnectionPool is, so this is the
        # right choice now even though nothing is threaded yet.
        #
        # minconn/maxconn are a starting guess sized for a small app on a
        # modest Render Postgres plan — revisit against Postgres's own
        # max_connections if concurrent load grows.
        self._pool = ThreadedConnectionPool(
            minconn=2, maxconn=10, cursor_factory=RealDictCursor, **self.db_config
        )

    def _execute(self, query: str, params: tuple = (), fetch: str = 'one'):
        # Borrow a connection from the pool instead of opening a fresh one.
        # `with conn:` still handles the transaction (commits on clean exit,
        # rolls back on exception) exactly as before — it just no longer
        # closes the connection, since putconn() in the finally block returns
        # it to the pool for reuse instead.
        conn = self._pool.getconn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    if fetch == 'one': return cur.fetchone()
                    if fetch == 'all': return cur.fetchall()
                    return None
        finally:
            self._pool.putconn(conn)

    # --- PEOPLE ---

    def get_person_by_external_id(self, external_id: str) -> Optional[Dict]:
        return self._execute("SELECT * FROM people WHERE external_id = %s;", (str(external_id),))

    def get_person_by_id(self, person_id: str) -> Optional[Dict]:
        return self._execute("SELECT * FROM people WHERE id = %s;", (person_id,))

    def person_has_clinical_access(self, person_id: str) -> bool:
        """
        Whether this person can see clinical records (medications, doctors,
        etc.) anywhere in the app. Purely person-scoped, independent of
        which circle(s) they belong to or which circle they're currently
        viewing — e.g. an outer-circle-only member with real clinical
        authority (e.g. Power of Attorney) can have this set true, while an
        inner circle member could in principle have it false, though in
        practice it'll be set true for all core family members at
        person-creation time. No circle-membership join at all — this is
        just people.clinical_access, decided once per person, independent of
        circle structure.
        """
        row = self._execute(
            "SELECT clinical_access FROM people WHERE id = %s;", (str(person_id),)
        )
        return bool(row and row.get('clinical_access'))

    def update_person(self, person_id: str, name: Optional[str] = None,
                        phone: Optional[str] = None, email: Optional[str] = None,
                        aliases: Optional[List[str]] = None, notes: Optional[str] = None,
                        external_id: Optional[str] = None, date_of_birth: Optional[str] = None,
                        clinical_access: Optional[bool] = None) -> Dict:
        """
        clinical_access: None leaves the existing value unchanged (same
        COALESCE pattern as the other fields) — pass True/False explicitly
        to change it. Callers should restrict this to admin-only update
        paths; see /app/people/{person_id} in main.py.
        """
        query = """
            UPDATE people SET
                name          = COALESCE(%(name)s, name),
                phone         = COALESCE(%(phone)s, phone),
                email         = COALESCE(%(email)s, email),
                aliases       = COALESCE(%(aliases)s, aliases),
                notes         = COALESCE(%(notes)s, notes),
                external_id   = COALESCE(%(external_id)s, external_id),
                date_of_birth = %(date_of_birth)s,
                clinical_access = COALESCE(%(clinical_access)s, clinical_access)
            WHERE id = %(id)s
            RETURNING *;
        """
        return self._execute(query, {
            'id': person_id, 'name': name,
            'phone': phone, 'email': email,
            'aliases': aliases, 'notes': notes,
            'external_id': external_id,
            'date_of_birth': date_of_birth,
            'clinical_access': clinical_access,
        })

    # --- CHANNEL IDENTITIES (replaces people.external_id — see Trello #63) ---
    #
    # people.external_id is still read/written elsewhere in this file and in
    # take_five/integrations/groupme.py during the transition — these are the
    # new, general per-channel equivalents. Not yet wired into any caller;
    # that migration is scoped separately (call sites in groupme.py, plus
    # log_message/fetch_circle_roster/list_people_by_ensemble in this file).

    def get_person_by_channel_identity(self, channel: str, external_id: str) -> Optional[Dict]:
        """
        The per-channel replacement for get_person_by_external_id(). Used on
        the hot path (inbound webhook person lookup) — backed by the table's
        UNIQUE(channel, external_id) constraint, so this is a direct index
        lookup, not a scan.
        """
        return self._execute("""
            SELECT p.* FROM people p
            JOIN person_channel_identities pci ON pci.person_id = p.id
            WHERE pci.channel = %(channel)s AND pci.external_id = %(external_id)s;
        """, {'channel': channel, 'external_id': str(external_id)})

    def get_person_channel_identities(self, person_id: str) -> List[Dict]:
        """All known channel identities for a person — e.g. for an admin
        view showing which platforms someone is reachable on."""
        return self._execute("""
            SELECT * FROM person_channel_identities
            WHERE person_id = %(person_id)s
            ORDER BY channel;
        """, {'person_id': person_id}, fetch='all')

    def upsert_person_channel_identity(self, person_id: str, channel: str,
                                        external_id: str) -> Dict:
        """
        Link a person to a channel identity. ON CONFLICT (channel,
        external_id) DO NOTHING — this identity may already be linked to
        this exact person (safe re-add, e.g. re-running a backfill), but if
        it's linked to a *different* person that's a real conflict (the
        same GroupMe account somehow matched two people rows) that should
        surface, not silently overwrite who owns the identity. Mirrors the
        carefulness in add_person_to_groupme()'s own external_id backfill,
        which never clobbers an existing value either.

        Raises ValueError if the identity is already linked to a different
        person.
        """
        existing = self._execute("""
            SELECT person_id FROM person_channel_identities
            WHERE channel = %(channel)s AND external_id = %(external_id)s;
        """, {'channel': channel, 'external_id': str(external_id)})
        if existing and str(existing['person_id']) != str(person_id):
            raise ValueError(
                f"Channel identity {channel}:{external_id} is already linked to a "
                f"different person ({existing['person_id']}), not {person_id}"
            )
        return self._execute("""
            INSERT INTO person_channel_identities (person_id, channel, external_id)
            VALUES (%(person_id)s, %(channel)s, %(external_id)s)
            ON CONFLICT (channel, external_id) DO NOTHING
            RETURNING *;
        """, {'person_id': person_id, 'channel': channel, 'external_id': str(external_id)}) or existing

    def get_person_channel_credential(self, person_id: str, channel: str) -> Optional[Dict]:
        """
        Stored access token for a person on a channel — currently only
        GroupMe populates this (per-admin OAuth token, card #39). Most
        channels (WhatsApp, default-case email) never have a row here; their
        credentials are platform-level app config, not person-scoped. See
        migration 010_channel_identities.sql for the reasoning.
        """
        return self._execute("""
            SELECT * FROM person_channel_credentials
            WHERE person_id = %(person_id)s AND channel = %(channel)s;
        """, {'person_id': person_id, 'channel': channel})

    def upsert_person_channel_credential(self, person_id: str, channel: str,
                                          access_token: str) -> Dict:
        """
        Store or replace a person's credential for a channel. A fresh OAuth
        login for the same person/channel replaces the prior token rather
        than adding a second row — backed by UNIQUE(person_id, channel).
        Confirmed safe for GroupMe specifically: a new token for the same
        account carries identical permissions to the old one, so replacing
        rather than appending loses nothing (see card #39 design notes).
        """
        return self._execute("""
            INSERT INTO person_channel_credentials (person_id, channel, access_token)
            VALUES (%(person_id)s, %(channel)s, %(access_token)s)
            ON CONFLICT (person_id, channel) DO UPDATE SET
                access_token = EXCLUDED.access_token,
                obtained_at = now()
            RETURNING *;
        """, {'person_id': person_id, 'channel': channel, 'access_token': access_token})

    def add_person_to_ensemble(self, ensemble_id: str, name: str, **kwargs) -> Dict:
        """
        clinical_access (kwarg, default False): decided once, here, at
        person-creation time — independent of whatever circle(s) they're
        later assigned to. See person_has_clinical_access().
        """
        query = """
            INSERT INTO people (ensemble_id, name, phone, email, timezone, aliases, notes, external_id, date_of_birth, clinical_access)
            VALUES (%(ensemble_id)s, %(name)s, %(phone)s, %(email)s, %(tz)s, %(aliases)s, %(notes)s, %(external_id)s, %(dob)s, %(clinical_access)s)
            RETURNING *;
        """
        return self._execute(query, {
            'ensemble_id': ensemble_id, 'name': name,
            'phone': kwargs.get('phone'), 'email': kwargs.get('email'),
            'tz': kwargs.get('timezone', 'America/Chicago'),
            'aliases': kwargs.get('aliases', []), 'notes': kwargs.get('notes'),
            'external_id': kwargs.get('external_id'),
            'dob': kwargs.get('date_of_birth'),
            'clinical_access': kwargs.get('clinical_access', False),
        })

    # --- LEADS ---

    def create_lead(self, lead_type: str, name: str, email: str,
                     phone: Optional[str] = None, details: Optional[Dict] = None,
                     source: Optional[str] = None) -> Dict:
        query = """
            INSERT INTO leads (lead_type, name, email, phone, details, source)
            VALUES (%(lead_type)s, %(name)s, %(email)s, %(phone)s, %(details)s, %(source)s)
            RETURNING *;
        """
        return self._execute(query, {
            'lead_type': lead_type, 'name': name, 'email': email,
            'phone': phone, 'details': Json(details or {}), 'source': source,
        })

    # --- CARE CIRCLES ---

    def create_care_circle(self, ensemble_id: str, name: str, status: str = 'active',
                           external_id: Optional[str] = None,
                           parent_circle_id: Optional[str] = None) -> Dict:
        """
        parent_circle_id: NULL (default) creates a top-level/inner circle.
        Passing a value creates an outer circle pointing at that inner
        circle. One level of nesting only, enforced here rather than as a
        DB constraint (no clean way to express a cross-row "can't be a
        parent if you have a parent" rule as a CHECK) — raises ValueError if
        parent_circle_id itself already has a parent.
        """
        if parent_circle_id is not None:
            parent = self.get_circle_by_id(parent_circle_id)
            if parent is None:
                raise ValueError(f"parent_circle_id {parent_circle_id} does not exist")
            if parent.get('parent_circle_id') is not None:
                raise ValueError(
                    f"Cannot nest under circle {parent_circle_id} — it is itself an "
                    f"outer circle. Only one level of circle nesting is supported."
                )

        query = """
            INSERT INTO care_circles (ensemble_id, name, status, external_id, parent_circle_id)
            VALUES (%(ensemble_id)s, %(name)s, %(status)s, %(external_id)s, %(parent_circle_id)s)
            RETURNING *;
        """
        return self._execute(query, {
            'ensemble_id': ensemble_id, 'name': name,
            'status': status, 'external_id': external_id,
            'parent_circle_id': parent_circle_id,
        })

    def get_active_circles(self) -> List[Dict]:
        return self._execute(
            "SELECT * FROM care_circles WHERE status = 'active' ORDER BY created_at;",
            fetch='all'
        )

    def update_care_circle(self, circle_id: str, updates: dict) -> Dict:
        query = """
            UPDATE care_circles SET
                name               = COALESCE(%(name)s, name),
                status             = COALESCE(%(status)s, status),
                external_id        = COALESCE(%(external_id)s, external_id),
                integration_config = COALESCE(%(integration_config)s, integration_config)
            WHERE id = %(id)s
            RETURNING *;
        """
        return self._execute(query, {
            'id': circle_id,
            'name': updates.get('name'),
            'status': updates.get('status'),
            'external_id': updates.get('external_id'),
            'integration_config': Json(updates['integration_config'])
                if updates.get('integration_config') is not None else None,
        })

    def get_circle_by_external_id(self, external_id: str) -> Optional[Dict]:
        return self._execute(
            "SELECT * FROM care_circles WHERE external_id = %s;", (str(external_id),)
        )

    def get_readable_circle_ids(self, circle_id: str) -> List[str]:
        """
        Resolve the readable circle set for a given circle — the set of
        circle_ids whose messages/digest content this circle is allowed to
        read. Same query works unmodified for both inner and outer circles,
        no branching required:

        - Called with an outer circle's id: parent_circle_id points at an
          inner circle, and nothing has an outer circle as ITS parent (one
          level of nesting only), so this returns just the outer circle
          itself.
        - Called with an inner circle's id (parent_circle_id IS NULL): this
          returns itself plus every circle whose parent_circle_id points at
          it — i.e. itself plus all its outer circle(s).

        Used by ask() and digest generation, which are allowed to read
        across the inner/outer boundary. NOT used by the single-circle
        engagement/proactive features (Life Log gap detection, post-visit
        follow-up, prep packets) — those stay scoped to whichever one circle
        they're running for; see call sites for [circle_id] one-item lists
        instead of this resolver.
        """
        rows = self._execute("""
            SELECT id FROM care_circles WHERE id = %(circle_id)s
            UNION
            SELECT id FROM care_circles WHERE parent_circle_id = %(circle_id)s;
        """, {"circle_id": str(circle_id)}, fetch="all")
        return [str(r["id"]) for r in (rows or [])]

    def find_active_sms_members_by_phone(self, phone: str) -> List[Dict]:
        """
        Find every active care circle a phone number can text into, one row
        per circle. Deduplicated by circle: the same phone can be sms_active
        via more than one person row in the same circle (e.g. a tester
        playing several roles), and that shouldn't produce duplicate options.

        Take Five uses a single shared Twilio number for the whole platform,
        so identity comes from who is texting (From), not which number they
        texted (To). This normally returns exactly one row. More than one
        means the phone is active in more than one circle — the caller is
        responsible for disambiguating before treating the message as a care
        update. Ordered by ensemble then circle name so a disambiguation
        prompt's numbering is stable across calls, and includes ensemble_name
        so circles with similar names across different families can still be
        told apart.
        """
        return self._execute("""
            SELECT * FROM (
                SELECT DISTINCT ON (cc.id)
                       p.*, cm.role, cm.sms_active,
                       cc.id AS circle_id, cc.name AS circle_name,
                       cc.external_id AS circle_external_id,
                       cc.integration_config AS circle_integration_config,
                       e.name AS ensemble_name
                FROM people p
                JOIN circle_memberships cm ON p.id = cm.person_id
                JOIN care_circles cc ON cc.id = cm.circle_id
                JOIN ensembles e ON e.id = cc.ensemble_id
                WHERE p.phone = %(phone)s
                  AND cm.sms_active = true
                  AND cc.status = 'active'
                ORDER BY cc.id, p.name
            ) sub
            ORDER BY ensemble_name, circle_name;
        """, {'phone': phone}, fetch='all')

    def get_circle_by_id(self, circle_id: str) -> Optional[Dict]:
        return self._execute(
            "SELECT * FROM care_circles WHERE id = %s;", (str(circle_id),)
        )

    def fetch_circle_roster(self, circle_id: str) -> list:
        query = """
            SELECT
                p.id,
                p.name          AS member_name,
                p.phone,
                p.email,
                p.aliases       AS person_aliases,
                p.notes         AS person_notes,
                p.external_id,
                cm.role                AS person_role,
                cm.chat_membership_id,
                cm.chat_added_at,
                c.name                 AS circle_name,
                COUNT(m.id)            AS msg_count,
                MAX(m.sent_at)         AS last_active
            FROM care_circles c
            JOIN circle_memberships cm ON c.id = cm.circle_id
            JOIN people p ON cm.person_id = p.id
            LEFT JOIN messages m
                ON m.circle_id = c.id
               AND m.person_id = p.id
               AND m.direction = 'inbound'
            WHERE c.id = %(circle_id)s
            GROUP BY p.id, p.name, p.phone, p.email,
                     p.aliases, p.notes, p.external_id,
                     cm.role, cm.chat_membership_id, cm.chat_added_at, c.name
            ORDER BY cm.role, msg_count DESC
        """
        return self._execute(query, {"circle_id": circle_id}, fetch="all")

    def get_seniors_in_circle(self, circle_id: str) -> List[Dict]:
        """
        Return all people with role='senior' in a circle.
        Used by ask_with_tools() to resolve care recipient when
        the label has no patient name.
        """
        query = """
            SELECT p.id, p.name, p.aliases
            FROM people p
            JOIN circle_memberships cm ON p.id = cm.person_id
            WHERE cm.circle_id = %(circle_id)s
              AND cm.role = 'senior'
            ORDER BY p.name;
        """
        return self._execute(query, {"circle_id": circle_id}, fetch="all")

    # --- MEMBERSHIPS ---

    def list_care_circles(self, ensemble_id: str) -> List[Dict]:
        return self._execute(
            "SELECT * FROM care_circles WHERE ensemble_id = %s ORDER BY name;",
            (ensemble_id,), fetch='all'
        )

    def add_person_to_circle(self, circle_id: str, person_id: str, role: str) -> Dict:
        query = """
            INSERT INTO circle_memberships (circle_id, person_id, role)
            VALUES (%(circle_id)s, %(person_id)s, %(role)s)
            ON CONFLICT (circle_id, person_id) DO UPDATE SET role = EXCLUDED.role
            RETURNING *;
        """
        return self._execute(query, {
            'circle_id': circle_id, 'person_id': person_id, 'role': role
        })

    def get_circle_membership(self, circle_id: str, person_id: str) -> Optional[Dict]:
        return self._execute("""
            SELECT * FROM circle_memberships
            WHERE circle_id = %(circle_id)s AND person_id = %(person_id)s;
        """, {'circle_id': circle_id, 'person_id': person_id})

    def record_chat_membership(self, circle_id: str, person_id: str,
                                chat_membership_id: Optional[str]) -> Optional[Dict]:
        """
        Marks a circle_membership as added to the circle's chat platform
        (GroupMe today, others later — see take_five/integrations/chat.py).
        chat_added_at is set to now() regardless of whether chat_membership_id
        was resolved (see add_person_to_groupme's polling note — the add can
        succeed even when the platform doesn't hand back a confirmed id in
        time). Explicit, per-person action — never called automatically from
        add_person_to_circle(). See migration 009_chat_membership.sql.
        """
        return self._execute("""
            UPDATE circle_memberships SET
                chat_membership_id = %(chat_membership_id)s,
                chat_added_at = now()
            WHERE circle_id = %(circle_id)s AND person_id = %(person_id)s
            RETURNING *;
        """, {
            'circle_id': circle_id, 'person_id': person_id,
            'chat_membership_id': chat_membership_id,
        })

    def clear_chat_membership(self, circle_id: str, person_id: str) -> Optional[Dict]:
        """
        Reverses record_chat_membership — called after successfully removing
        someone from the circle's chat platform. Resets both columns to NULL
        so the roster correctly shows them as not-in-chat again (and the
        admin UI's "Add to GroupMe" button reappears for them). Does NOT
        touch people.external_id — that's the person's platform identity,
        which stays valid even after being removed from one circle's chat.
        """
        return self._execute("""
            UPDATE circle_memberships SET
                chat_membership_id = NULL,
                chat_added_at = NULL
            WHERE circle_id = %(circle_id)s AND person_id = %(person_id)s
            RETURNING *;
        """, {'circle_id': circle_id, 'person_id': person_id})

    def remove_person_from_circle(self, circle_id: str, person_id: str) -> None:
        self._execute("""
            DELETE FROM circle_memberships
            WHERE circle_id = %(circle_id)s AND person_id = %(person_id)s;
        """, {'circle_id': circle_id, 'person_id': person_id}, fetch=None)

    # --- MESSAGES ---

    def log_message(self, circle_ext_id: str, person_ext_id: Optional[str],
                    body: str, msg_type: str = 'inbound',
                    direction: str = 'inbound', raw_data: Optional[Dict] = None,
                    channel: str = 'groupme',
                    person_id: Optional[str] = None) -> Dict:
        """
        Logs a message to the messages table.

        person_id: pass a UUID directly to bypass the external_id subquery.
                   Takes precedence over person_ext_id when both are provided.
        person_ext_id=None for bot/agent outbound messages — person_id is
        inserted as NULL directly rather than via subquery.

        Semantics:
          direction='inbound',  person_id=<uuid> → human message
          direction='outbound', person_id=NULL   → bot/agent message
        """
        if person_id:
            query = """
                INSERT INTO messages (circle_id, person_id, message_type, direction, body, raw, channel)
                VALUES (
                    (SELECT id FROM care_circles WHERE external_id = %s),
                    %s,
                    %s, %s, %s, %s, %s
                ) RETURNING *;
            """
            params = (
                str(circle_ext_id), str(person_id),
                msg_type, direction, body,
                Json(raw_data) if raw_data else None, channel,
            )
        elif person_ext_id:
            query = """
                INSERT INTO messages (circle_id, person_id, message_type, direction, body, raw, channel)
                VALUES (
                    (SELECT id FROM care_circles WHERE external_id = %s),
                    (SELECT id FROM people WHERE external_id = %s),
                    %s, %s, %s, %s, %s
                ) RETURNING *;
            """
            params = (
                str(circle_ext_id), str(person_ext_id),
                msg_type, direction, body,
                Json(raw_data) if raw_data else None, channel,
            )
        else:
            query = """
                INSERT INTO messages (circle_id, person_id, message_type, direction, body, raw, channel)
                VALUES (
                    (SELECT id FROM care_circles WHERE external_id = %s),
                    NULL,
                    %s, %s, %s, %s, %s
                ) RETURNING *;
            """
            params = (
                str(circle_ext_id),
                msg_type, direction, body,
                Json(raw_data) if raw_data else None, channel,
            )
        return self._execute(query, params)

    def insert_reference_messages(self, circle_id: str, channel: str,
                                   thread_label: str, entries: List[Dict]) -> List[Dict]:
        """
        Backfills external reference content (email threads, documents) added
        manually via the admin References tab. Unlike log_message, this takes
        an internal circle_id directly (admin already has it, no external_id
        subquery needed) and an explicit historical sent_at per entry, since
        this content predates ingestion.

        entries: list of dicts with keys person_id (optional), external_name
                 (optional, used when person_id is None), external_org
                 (optional), sent_at (datetime), body (str).

        message_type is fixed to 'external_reference' so the digest generator
        can exclude this content from "what happened this week" summaries
        while ask() and decision support can still retrieve it.
        """
        inserted = []
        for entry in entries:
            raw_data = {
                'thread_label': thread_label,
                'external_name': entry.get('external_name'),
                'external_org': entry.get('external_org'),
            }
            query = """
                INSERT INTO messages (circle_id, person_id, message_type, direction, body, raw, channel, sent_at)
                VALUES (%s, %s, 'external_reference', 'inbound', %s, %s, %s, %s)
                RETURNING *;
            """
            params = (
                str(circle_id), entry.get('person_id'), entry['body'],
                Json(raw_data), channel, entry['sent_at'],
            )
            inserted.append(self._execute(query, params))
        return inserted

    def get_messages(self, circle_ids: List[str], start_date: datetime = None,
                     end_date: datetime = None, limit: int = None) -> List[Dict]:
        """
        Fetch messages for one or more circles. Bot messages (person_id IS
        NULL) are labelled 'Take Five' so ask() can identify them in context.

        circle_ids is always a list — single-circle callers pass a one-item
        list (e.g. [circle_id]) rather than a bare string. Multi-circle
        callers (inner circle's ask()/digest, which also read their outer
        circle's messages) resolve the readable circle set first via
        get_readable_circle_ids() and pass that list straight in. Keeping
        the signature always-plural means this function never needs to know
        or care why the list has one item or several.
        """
        query = """
            SELECT
                m.*,
                COALESCE(p.name, m.raw->>'external_name', 'Take Five') AS author_name
            FROM messages m
            LEFT JOIN people p ON m.person_id = p.id
            WHERE m.circle_id = ANY(%s::uuid[])
        """
        params = [[str(c) for c in circle_ids]]

        if start_date:
            query += " AND m.sent_at >= %s"
            params.append(start_date)
        if end_date:
            query += " AND m.sent_at <= %s"
            params.append(end_date)

        query += " ORDER BY m.sent_at DESC"

        if limit:
            query += " LIMIT %s"
            params.append(limit)

        return self._execute(query, tuple(params), fetch='all')

    def get_reference_threads(self, circle_id: str) -> List[Dict]:
        """
        Distinct thread labels already logged for a circle via the admin
        References tab, with a count and most recent sent_at — powers the
        "already logged for this thread" panel so an admin can see where
        they left off before pasting more of a thread.
        """
        return self._execute("""
            SELECT
                raw->>'thread_label' AS thread_label,
                COUNT(*) AS message_count,
                MAX(sent_at) AS last_sent_at
            FROM messages
            WHERE circle_id = %(circle_id)s
              AND message_type = 'external_reference'
            GROUP BY raw->>'thread_label'
            ORDER BY MAX(sent_at) DESC;
        """, {'circle_id': str(circle_id)}, fetch='all')

    def get_reference_messages(self, circle_id: str, thread_label: str) -> List[Dict]:
        """Messages already logged for one thread label, oldest first."""
        return self._execute("""
            SELECT
                m.sent_at,
                COALESCE(p.name, m.raw->>'external_name') AS sender_name,
                (m.person_id IS NULL) AS is_external,
                m.body
            FROM messages m
            LEFT JOIN people p ON m.person_id = p.id
            WHERE m.circle_id = %(circle_id)s
              AND m.message_type = 'external_reference'
              AND m.raw->>'thread_label' = %(thread_label)s
            ORDER BY m.sent_at ASC;
        """, {'circle_id': str(circle_id), 'thread_label': thread_label}, fetch='all')

    def get_message_by_id(self, message_id: str) -> Optional[Dict]:
        """Fetch a single message with author_name resolved, for citing the
        exact source (sender, sent_at, body) of a signal or a corroboration
        ask — avoids relying on an LLM's recall of broader chat history."""
        return self._execute("""
            SELECT
                m.*,
                COALESCE(p.name, m.raw->>'external_name', 'Take Five') AS author_name
            FROM messages m
            LEFT JOIN people p ON m.person_id = p.id
            WHERE m.id = %(message_id)s;
        """, {'message_id': str(message_id)})

    def get_recent_context_messages(self, circle_id: str, before_message_id: str,
                                     limit: int = 20) -> List[Dict]:
        """
        Last `limit` inbound messages strictly before the given message,
        oldest first. Used by clinical signal detection to resolve ambiguous
        or anaphoric references ("the big toe", "her follow-up") that name
        no subject in the message being analyzed but refer back to something
        said earlier in the circle's chat. Context only — never a source of
        new signals itself, since each of these messages was already
        processed in its own turn.
        """
        rows = self._execute("""
            SELECT
                m.sent_at,
                COALESCE(p.name, m.raw->>'external_name', 'Take Five') AS author_name,
                m.body
            FROM messages m
            LEFT JOIN people p ON m.person_id = p.id
            WHERE m.circle_id = %(circle_id)s
              AND m.direction = 'inbound'
              AND m.sent_at < (SELECT sent_at FROM messages WHERE id = %(before_message_id)s)
            ORDER BY m.sent_at DESC
            LIMIT %(limit)s;
        """, {
            'circle_id': str(circle_id),
            'before_message_id': str(before_message_id),
            'limit': limit,
        }, fetch='all')
        return list(reversed(rows)) if rows else []

    def upsert_message_chunk(self, message_id: str, circle_id: str, chunk_index: int,
                              body: str, context_header: str, context_summary: str,
                              embedded_text: str, embedding: list, sent_at) -> Dict:
        query = """
            INSERT INTO message_chunks
                (message_id, circle_id, chunk_index, body,
                 context_header, context_summary, embedded_text, embedding, sent_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
            ON CONFLICT (message_id, chunk_index) DO UPDATE SET
                context_summary = EXCLUDED.context_summary,
                embedded_text   = EXCLUDED.embedded_text,
                embedding       = EXCLUDED.embedding
            RETURNING *;
        """
        return self._execute(query, (
            message_id, circle_id, chunk_index, body,
            context_header, context_summary, embedded_text,
            str(embedding), sent_at
        ))

    def fetch_semantic_chunks(self, circle_ids: list[str], question_embedding: list[float],
                               limit: int = 10) -> list:
        """
        circle_ids is always a list, same always-plural pattern as
        get_messages() — single-circle callers pass a one-item list;
        multi-circle callers (inner circle reading its outer circle too)
        pass the resolved readable circle set from get_readable_circle_ids().
        """
        query = """
            SELECT
                mc.body,
                mc.context_header,
                mc.context_summary,
                mc.sent_at,
                1 - (mc.embedding <=> %(embedding)s::vector) AS similarity
            FROM message_chunks mc
            JOIN care_circles c ON mc.circle_id = c.id
            WHERE c.id = ANY(%(circle_ids)s::uuid[])
            ORDER BY mc.embedding <=> %(embedding)s::vector
            LIMIT %(limit)s
        """
        return self._execute(
            query,
            {"embedding": str(question_embedding),
             "circle_ids": [str(c) for c in circle_ids], "limit": limit},
            fetch="all",
        )

    # --- CLINICAL SIGNALS ---

    def save_clinical_signal(
        self,
        message_id: str,
        circle_id: str,
        signal_category: str,
        signal_type: str,
        subject_id: Optional[str] = None,
        raw_excerpt: Optional[str] = None,
        mention_style: Optional[str] = None,
        confidence: Optional[float] = None,
        channel: str = "groupme",
        request_corroboration: bool = False,
        superseded_by_id: Optional[str] = None,
    ) -> Dict:
        """
        Insert a clinical signal record.
        Called by the signal detection agent post-message-storage.
        """
        query = """
            INSERT INTO clinical_signals (
                message_id, circle_id, subject_id,
                signal_category, signal_type,
                raw_excerpt, mention_style, confidence,
                channel, request_corroboration,
                superseded_by_id
            ) VALUES (
                %(message_id)s, %(circle_id)s, %(subject_id)s,
                %(signal_category)s, %(signal_type)s,
                %(raw_excerpt)s, %(mention_style)s, %(confidence)s,
                %(channel)s, %(request_corroboration)s,
                %(superseded_by_id)s
            )
            RETURNING *;
        """
        return self._execute(query, {
            "message_id":             message_id,
            "circle_id":              circle_id,
            "subject_id":             subject_id,
            "signal_category":        signal_category,
            "signal_type":            signal_type,
            "raw_excerpt":            raw_excerpt,
            "mention_style":          mention_style,
            "confidence":             confidence,
            "channel":                channel,
            "request_corroboration":  request_corroboration,
            "superseded_by_id":       superseded_by_id,
        }, fetch="one")

    def get_pending_corroboration_signals(self, circle_id: str, max_age_days: int = 7,
                                            as_of: Optional[datetime] = None) -> List[Dict]:
        """
        Signals flagged as corroboration candidates that have never been asked
        about. Ask-once model: once corroboration_requested_at is stamped, a
        signal drops out of this list for good — no re-nudging, no resolution
        tracking. Oldest-first, so the longest-waiting eligible signal surfaces
        first.

        Bounded by max_age_days: candidates older than the window are never
        surfaced, not just deprioritized. Asking about something weeks old
        feels disconnected from the conversation it came from, and this also
        keeps a one-time historical backlog from dominating the queue once
        this check goes live — aging out unasked is an acceptable outcome
        under the ask-once model, same as never getting a reply.

        as_of: reference time to evaluate against instead of the real current
        time — lets the engagement cron be tested against a simulated "now"
        (see main_engagement.py --as-of) without waiting for real time to
        pass. Also excludes signals detected after as_of, since in a
        simulated past they wouldn't exist yet. Defaults to real now().
        """
        reference_time = as_of or datetime.now(timezone.utc)
        query = """
            SELECT cs.*, p.name AS subject_name
            FROM clinical_signals cs
            LEFT JOIN people p ON p.id = cs.subject_id
            WHERE cs.circle_id = %(circle_id)s
              AND cs.request_corroboration = true
              AND cs.corroboration_requested_at IS NULL
              AND cs.detected_at >= %(reference_time)s - make_interval(days => %(max_age_days)s)
              AND cs.detected_at <= %(reference_time)s
            ORDER BY cs.detected_at ASC;
        """
        return self._execute(
            query,
            {"circle_id": str(circle_id), "max_age_days": max_age_days, "reference_time": reference_time},
            fetch="all",
        )

    def mark_corroboration_requested(self, signal_id: str) -> Dict:
        """Stamps corroboration_requested_at — the terminal state for check 2."""
        query = """
            UPDATE clinical_signals
            SET corroboration_requested_at = now()
            WHERE id = %(id)s
            RETURNING *;
        """
        return self._execute(query, {"id": str(signal_id)}, fetch="one")

    def get_last_engagement_activity(self, circle_id: str,
                                       as_of: Optional[datetime] = None) -> Optional[datetime]:
        """
        Most recent timestamp counting as "engagement" for the Life Log gap
        check (take_five/engagement/checks.py, Tier 2): any inbound
        circle-member message, or any prior T5 check-in
        (message_type='check_in', covers both the clinical signal
        corroboration check and Life Log itself).

        Deliberately excludes 'digest' and 'prep_packet' — the weekly digest
        does not count as an engagement touch per the card's design (a family
        that never talks but gets a digest is still in a lull).

        as_of: bounds the search to activity at or before this time — lets
        the engagement cron be tested against a simulated "now" (see
        main_engagement.py --as-of) without waiting for real time to pass, and
        without real messages sent after as_of leaking into the gap
        calculation. Defaults to real now().

        No new table — computed on the fly from existing message timestamps,
        consistent with current schema philosophy.
        """
        reference_time = as_of or datetime.now(timezone.utc)
        row = self._execute("""
            SELECT MAX(sent_at) AS last_activity
            FROM messages
            WHERE circle_id = %(circle_id)s
              AND sent_at <= %(reference_time)s
              AND (direction = 'inbound' OR message_type = 'check_in');
        """, {"circle_id": str(circle_id), "reference_time": reference_time})
        return row["last_activity"] if row else None

    # --- CLINICAL RECORDS ---

    def save_clinical_record(
        self,
        person_id: str,
        resource_type: str,
        data: Dict,
        notes: Optional[str] = None,
        status: str = 'active',
        confirmed_by: Optional[str] = None,
        source_message_id: Optional[str] = None,
        circle_id: Optional[str] = None,   # provenance only — which chat it came from
    ) -> Dict:
        """
        Insert a clinical record and write the initial 'added' event
        in a single transaction.

        resource_type: 'MedicationStatement' | 'Condition' | 'Observation'
                       'Appointment' | 'AllergyIntolerance' | 'Procedure'
                       'CareTeamMember'
        """
        confirmed_at = datetime.utcnow() if confirmed_by else None

        conn = self._pool.getconn()
        try:
            with conn:
                with conn.cursor() as cur:
                    # 1. Insert the clinical record
                    cur.execute("""
                        INSERT INTO clinical_records (
                            person_id, resource_type, status,
                            data, notes, confirmed_by, confirmed_at,
                            source_message_id, circle_id
                        ) VALUES (
                            %(person_id)s, %(resource_type)s, %(status)s,
                            %(data)s, %(notes)s, %(confirmed_by)s,
                            %(confirmed_at)s, %(source_message_id)s, %(circle_id)s
                        ) RETURNING *;
                    """, {
                        'person_id':         person_id,
                        'resource_type':     resource_type,
                        'status':            status,
                        'data':              Json(data),
                        'notes':             notes,
                        'confirmed_by':      confirmed_by,
                        'confirmed_at':      confirmed_at,
                        'source_message_id': source_message_id,
                        'circle_id':         circle_id,
                    })
                    record = cur.fetchone()

                    # 2. Write the 'added' event in the same transaction
                    cur.execute("""
                        INSERT INTO clinical_events (
                            record_id, event_type, notes,
                            confirmed_by, confirmed_at, source_message_id
                        ) VALUES (
                            %(record_id)s, 'added', %(notes)s,
                            %(confirmed_by)s, %(confirmed_at)s, %(source_message_id)s
                        );
                    """, {
                        'record_id':         record['id'],
                        'notes':             notes,
                        'confirmed_by':      confirmed_by,
                        'confirmed_at':      confirmed_at,
                        'source_message_id': source_message_id,
                    })

                    return record
        finally:
            self._pool.putconn(conn)

    def update_clinical_record(
        self,
        record_id: str,
        data: Optional[Dict] = None,
        notes: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict:
        """
        Simple field patcher — used by the admin API endpoint only.
        Does not write a clinical_event. For event-aware updates from
        the chat pipeline, use patch_clinical_record().
        """
        query = """
            UPDATE clinical_records SET
                data   = COALESCE(%(data)s,   data),
                notes  = %(notes)s,
                status = COALESCE(%(status)s, status)
            WHERE id = %(id)s
            RETURNING *;
        """
        return self._execute(query, {
            'id':     record_id,
            'data':   Json(data) if data is not None else None,
            'notes':  notes,
            'status': status,
        })

    def patch_clinical_record(
        self,
        record_id: str,
        event_type: str,                        # 'updated' | 'refilled' | 'discontinued'
        updated_fields: Optional[Dict] = None,  # only the changed fields
        notes: Optional[str] = None,
        confirmed_by: Optional[str] = None,
        source_message_id: Optional[str] = None,
    ) -> Dict:
        """
        Update a clinical record and write the corresponding clinical_event
        in a single transaction.

        event_type='updated':      pass updated_fields with only the changed fields.
                                   Diff is computed and stored in the event.
        event_type='refilled':     updated_fields is None — record unchanged,
                                   event is the signal.
        event_type='discontinued': updated_fields is None — record status set to
                                   'discontinued'.
        """
        confirmed_at = datetime.utcnow() if confirmed_by else None

        conn = self._pool.getconn()
        try:
            with conn:
                with conn.cursor() as cur:

                    # 1. Fetch current record for diff
                    cur.execute(
                        "SELECT * FROM clinical_records WHERE id = %s FOR UPDATE;",
                        (record_id,)
                    )
                    current = cur.fetchone()
                    if not current:
                        raise ValueError(f"Clinical record {record_id} not found")

                    current_data = (
                        current['data']
                        if isinstance(current['data'], dict)
                        else json.loads(current['data'])
                    )

                    # 2. Apply updates to the record
                    if event_type == 'updated' and updated_fields:
                        previous_values = {
                            k: current_data.get(k)
                            for k in updated_fields
                        }
                        new_data = {**current_data, **updated_fields}
                        cur.execute("""
                            UPDATE clinical_records
                            SET data = %(data)s
                            WHERE id = %(id)s
                            RETURNING *;
                        """, {
                            'id':   record_id,
                            'data': Json(new_data),
                        })
                        record = cur.fetchone()

                    elif event_type == 'discontinued':
                        previous_values = None
                        cur.execute("""
                            UPDATE clinical_records
                            SET status = 'discontinued'
                            WHERE id = %(id)s
                            RETURNING *;
                        """, {'id': record_id})
                        record = cur.fetchone()

                    else:
                        # refilled — record data unchanged
                        previous_values = None
                        record = current

                    # 3. Write the event
                    cur.execute("""
                        INSERT INTO clinical_events (
                            record_id, event_type,
                            changed_fields, previous_values,
                            notes, confirmed_by, confirmed_at,
                            source_message_id
                        ) VALUES (
                            %(record_id)s, %(event_type)s,
                            %(changed_fields)s, %(previous_values)s,
                            %(notes)s, %(confirmed_by)s, %(confirmed_at)s,
                            %(source_message_id)s
                        );
                    """, {
                        'record_id':       record_id,
                        'event_type':      event_type,
                        'changed_fields':  Json(updated_fields) if updated_fields else None,
                        'previous_values': Json(previous_values) if previous_values else None,
                        'notes':           notes,
                        'confirmed_by':    confirmed_by,
                        'confirmed_at':    confirmed_at,
                        'source_message_id': source_message_id,
                    })

                    return record
        finally:
            self._pool.putconn(conn)

    def get_clinical_events(self, record_id: str) -> List[Dict]:
        """Fetch the full event history for a clinical record, oldest first."""
        return self._execute("""
            SELECT
                ce.*,
                p.name AS confirmed_by_name
            FROM clinical_events ce
            LEFT JOIN people p ON ce.confirmed_by = p.id
            WHERE ce.record_id = %s
            ORDER BY ce.created_at ASC;
        """, (record_id,), fetch='all')

    def get_clinical_records(
        self,
        person_id: str,
        resource_type: Optional[str] = None,
        status: str = 'active',
    ) -> List[Dict]:
        """Fetch clinical records for a person, optionally filtered by type and status."""
        query = """
            SELECT cr.*, p.name AS person_name
            FROM clinical_records cr
            JOIN people p ON cr.person_id = p.id
            WHERE cr.person_id = %(person_id)s
              AND cr.status = %(status)s
        """
        params: Dict = {'person_id': person_id, 'status': status}

        if resource_type:
            query += " AND cr.resource_type = %(resource_type)s"
            params['resource_type'] = resource_type

        query += " ORDER BY cr.created_at DESC"
        return self._execute(query, params, fetch='all')

    def circle_has_full_clinical_access(self, circle_id: str) -> bool:
        """
        True only if every current member of this circle has
        people.clinical_access = true. Used to gate clinical records in
        broadcast surfaces (ask()/digest/prep-packet generation) — a chat
        answer is seen by everyone in the room, so visibility has to be
        decided by who's actually in the circle, not by a fixed inner/outer
        label or by the asker's own permission alone. If even one member
        lacks clinical_access (e.g. Peggy/Tony/Kathy in an outer circle that
        also includes David, who personally has clinical_access), the whole
        circle is blocked — David's own access doesn't leak to the others.
        An empty circle vacuously returns true (NOT EXISTS over zero rows),
        which is harmless since nothing would be asking in an empty circle
        anyway. See card #44 and the outer-circle clinical-records exposure
        found during Landry pilot testing, 2026-07-30.
        """
        row = self._execute("""
            SELECT NOT EXISTS (
                SELECT 1 FROM circle_memberships cm
                JOIN people p ON p.id = cm.person_id
                WHERE cm.circle_id = %(circle_id)s
                  AND p.clinical_access = false
            ) AS all_have_access;
        """, {'circle_id': str(circle_id)})
        return bool(row and row.get('all_have_access'))

    def get_clinical_records_for_circle(
        self,
        circle_id: str,
        resource_type: Optional[str] = None,
        status: str = 'active',
        person_id: Optional[str] = None,
    ) -> List[Dict]:
        """
        Fetch clinical records for seniors in a circle.
        Resolves seniors via circle_memberships — does not filter by circle_id
        on the clinical_records table.

        Gated by circle_has_full_clinical_access(): if any current member of
        this circle lacks people.clinical_access, this returns nothing at
        all, regardless of caller or asker. See that function's docstring
        for why this is a circle-composition check rather than a fixed
        inner/outer label or a per-asker permission check — this backs
        ask()/digest/prep-packet generation, all of which post into a chat
        everyone in the circle can see.

        If person_id is provided, scopes to that one senior only (e.g. for a
        prep packet targeted at a single senior in a circle with multiple
        seniors). Otherwise returns records for every senior in the circle.
        """
        if not self.circle_has_full_clinical_access(circle_id):
            return []

        query = """
            SELECT cr.*, p.name AS person_name
            FROM clinical_records cr
            JOIN people p ON cr.person_id = p.id
            JOIN circle_memberships cm ON p.id = cm.person_id
            WHERE cm.circle_id = %(circle_id)s
              AND cm.role = 'senior'
              AND cr.status = %(status)s
        """
        params: Dict = {'circle_id': circle_id, 'status': status}

        if resource_type:
            query += " AND cr.resource_type = %(resource_type)s"
            params['resource_type'] = resource_type

        if person_id:
            query += " AND cr.person_id = %(person_id)s"
            params['person_id'] = person_id

        query += " ORDER BY p.name, cr.created_at DESC"
        return self._execute(query, params, fetch='all')

    def get_prep_packets(self, circle_id: str, limit: int = 20,
                         since: Optional[datetime] = None) -> List[Dict]:
        """
        Return prep packets for a circle, newest first.
        These are outbound messages with message_type='prep_packet'.
        Metadata (doctor, appointment_desc, appointment_date, lookback,
        senior_person_id, followup_status) lives in the raw JSONB column.

        since: only return packets sent at or after this time — used by
        take_five/engagement/post_visit.py to scan a rolling recent window
        (e.g. the last 7 days) rather than full history on every cron run.
        """
        query = """
            SELECT
                m.id,
                m.body,
                m.sent_at,
                m.raw
            FROM messages m
            WHERE m.circle_id = %(circle_id)s
              AND m.message_type = 'prep_packet'
        """
        params: Dict = {'circle_id': circle_id, 'limit': limit}
        if since:
            query += " AND m.sent_at >= %(since)s"
            params['since'] = since
        query += " ORDER BY m.sent_at DESC LIMIT %(limit)s;"
        return self._execute(query, params, fetch='all')

    def mark_prep_packet_followup(self, message_id: str, status: str) -> Dict:
        """
        Flags a prep_packet message's post-visit follow-up state in its own
        raw JSONB — no new table or column. status is 'asked' (we sent the
        follow-up ask) or 'covered' (someone already reported back organically
        before we got to it). Either way, take_five/engagement/post_visit.py
        never reconsiders this packet again once it's flagged.
        """
        return self._execute("""
            UPDATE messages
            SET raw = COALESCE(raw, '{}'::jsonb) || %(patch)s::jsonb
            WHERE id = %(id)s
            RETURNING *;
        """, {"id": str(message_id), "patch": Json({"followup_status": status})}, fetch="one")

    # --- ENSEMBLES ---

    def create_ensemble(self, name: str, plan: str = 'family_plus', status: str = 'trial') -> Dict:
        query = """
            INSERT INTO ensembles (name, plan, status)
            VALUES (%(name)s, %(plan)s, %(status)s)
            RETURNING *;
        """
        return self._execute(query, {'name': name, 'plan': plan, 'status': status})

    def get_ensemble(self, ensemble_id: str) -> Optional[Dict]:
        return self._execute("SELECT * FROM ensembles WHERE id = %s;", (ensemble_id,))

    def get_ensemble_by_name(self, name: str) -> Optional[Dict]:
        return self._execute("SELECT * FROM ensembles WHERE name = %s;", (name,))

    def update_ensemble(self, ensemble_id: str, name: Optional[str] = None,
                        plan: Optional[str] = None, status: Optional[str] = None) -> Dict:
        """
        Patch ensemble fields. COALESCE pattern — only non-None values change.
        Setting status='archived' disables the family-admin page context for
        this ensemble's members (auth still resolves, but the ensemble is
        rendered inactive downstream).
        """
        query = """
            UPDATE ensembles SET
                name   = COALESCE(%(name)s, name),
                plan   = COALESCE(%(plan)s, plan),
                status = COALESCE(%(status)s, status)
            WHERE id = %(id)s
            RETURNING *;
        """
        return self._execute(query, {
            'id': ensemble_id, 'name': name, 'plan': plan, 'status': status,
        })

    def list_ensembles(self) -> List[Dict]:
        return self._execute(
            "SELECT * FROM ensembles ORDER BY created_at DESC;", fetch='all'
        )

    def list_people_by_ensemble(self, ensemble_id: str) -> List[Dict]:
        query = """
            SELECT
                p.id, p.ensemble_id, p.name,
                p.phone, p.email, p.aliases, p.notes,
                p.external_id, p.timezone, p.created_at,
                p.date_of_birth, p.clinical_access,
                COALESCE(em.user_role, 'member') AS user_role
            FROM people p
            LEFT JOIN ensemble_memberships em
                ON em.person_id = p.id
               AND em.ensemble_id = %(ensemble_id)s
            WHERE p.ensemble_id = %(ensemble_id)s
            ORDER BY p.name;
        """
        return self._execute(query, {'ensemble_id': ensemble_id}, fetch='all')

    def get_circle_topics(self, circle_id: str, limit: int = 200, days: int = None) -> Dict:
        """
        Keyword-category analysis + word frequency for trending topics
        and word cloud. Excludes outbound/bot messages and @T5 queries.
        """
        date_filter = "AND sent_at >= NOW() - INTERVAL '%(days)s days'" if days else ""
        base_params: dict = {'circle_id': circle_id, 'limit': limit}
        query = f"""
            SELECT body FROM messages
            WHERE circle_id = %(circle_id)s
              AND direction = 'inbound'
              AND body NOT ILIKE '%%@T5%%'
              AND LENGTH(body) > 10
              {date_filter}
            ORDER BY sent_at DESC
            LIMIT %(limit)s;
        """
        if days:
            base_params['days'] = days
        rows = self._execute(query, base_params, fetch='all')

        if not rows:
            return {'categories': [], 'word_freq': []}

        all_text = ' '.join(r['body'].lower() for r in rows)
        category_counts = {cat: 0 for cat in TOPIC_CATEGORIES}

        for row in rows:
            body_lower = row['body'].lower()
            for cat, keywords in TOPIC_CATEGORIES.items():
                if any(kw in body_lower for kw in keywords):
                    category_counts[cat] += 1

        # Word frequency — split on non-alpha, filter short/stopwords
        words = re.findall(r"[a-z']{3,}", all_text)
        freq = Counter(w for w in words if w not in TOPIC_STOPWORDS and len(w) > 3)
        top_words = [{'word': w, 'count': c} for w, c in freq.most_common(60)]

        categories = [
            {'category': cat, 'count': count}
            for cat, count in sorted(category_counts.items(), key=lambda x: -x[1])
            if count > 0
        ]

        return {'categories': categories, 'word_freq': top_words}

    def get_circle_analytics(self, circle_id: str, days: int = None) -> Dict:
        """Aggregate analytics for a single care circle."""
        params: dict = {'circle_id': circle_id}
        if days:
            params['days'] = days

        date_filter = "AND sent_at >= NOW() - INTERVAL '%(days)s days'" if days else ""

        weekly = self._execute(f"""
            SELECT
                DATE_TRUNC('week', sent_at AT TIME ZONE 'UTC') AS week,
                COUNT(CASE WHEN direction = 'inbound'  THEN 1 END) AS inbound,
                COUNT(CASE WHEN direction = 'outbound' THEN 1 END) AS outbound
            FROM messages
            WHERE circle_id = %(circle_id)s
              {date_filter}
            GROUP BY week
            ORDER BY week;
        """, params, fetch='all')

        hourly = self._execute(f"""
            SELECT
                EXTRACT(HOUR FROM sent_at AT TIME ZONE 'America/Chicago')::int AS hour,
                COUNT(*) AS msg_count
            FROM messages
            WHERE circle_id = %(circle_id)s
              AND direction = 'inbound'
              {date_filter}
            GROUP BY hour
            ORDER BY hour;
        """, params, fetch='all')

        members = self._execute(f"""
            SELECT
                p.name,
                cm.role,
                COUNT(m.id)                                                          AS msg_count,
                COUNT(CASE WHEN m.body ILIKE '%%@T5%%' THEN 1 END)                  AS bot_queries,
                MAX(m.sent_at)                                                       AS last_active
            FROM circle_memberships cm
            JOIN people p ON p.id = cm.person_id
            LEFT JOIN messages m
                ON m.circle_id = %(circle_id)s
               AND m.person_id = p.id
               AND m.direction = 'inbound'
               {date_filter}
            WHERE cm.circle_id = %(circle_id)s
            GROUP BY p.name, cm.role
            ORDER BY msg_count DESC;
        """, params, fetch='all')

        totals = self._execute(f"""
            SELECT
                COUNT(CASE WHEN direction = 'inbound'  THEN 1 END) AS total_inbound,
                COUNT(CASE WHEN direction = 'outbound' THEN 1 END) AS total_outbound,
                COUNT(CASE WHEN direction = 'inbound'
                            AND body ILIKE '%%@T5%%' THEN 1 END)   AS total_bot_queries,
                COUNT(DISTINCT DATE_TRUNC('day', sent_at))         AS active_days,
                MIN(sent_at)                                        AS first_message,
                MAX(sent_at)                                        AS last_message
            FROM messages
            WHERE circle_id = %(circle_id)s
              {date_filter};
        """, params)

        clinical = self._execute("""
            SELECT COUNT(*) AS total
            FROM clinical_records cr
            JOIN circle_memberships cm ON cr.person_id = cm.person_id
            WHERE cm.circle_id = %(circle_id)s
              AND cm.role = 'senior';
        """, {'circle_id': circle_id})

        return {
            'weekly':   [dict(r) for r in (weekly  or [])],
            'hourly':   [dict(r) for r in (hourly  or [])],
            'members':  [dict(r) for r in (members or [])],
            'totals':   dict(totals)   if totals   else {},
            'clinical': dict(clinical) if clinical else {'total': 0},
        }


    # --- USER-FACING (ensemble admin / member pages) ---

    _PERSON_MEMBERSHIP_SELECT = """
        SELECT
            p.id            AS person_id,
            p.name          AS person_name,
            p.email,
            p.phone,
            p.aliases,
            p.notes,
            p.date_of_birth,
            p.clinical_access,
            e.id            AS ensemble_id,
            e.name          AS ensemble_name,
            e.plan          AS ensemble_plan,
            e.status        AS ensemble_status,
            em.user_role
        FROM people p
        JOIN ensembles e ON p.ensemble_id = e.id
        JOIN ensemble_memberships em
            ON em.person_id = p.id
           AND em.ensemble_id = e.id
    """

    def lookup_people_by_phone(self, phone: str) -> List[Dict]:
        """
        Look up every person on (already-normalized, E.164) phone and return
        each one's ensemble membership context. Used by /auth/otp/request and
        /auth/otp/verify. Usually one row, but the same phone can belong to
        more than one person record (a shared household phone, a tester
        playing multiple roles) — same ambiguity find_active_sms_members_by_phone
        already handles for inbound SMS, mirrored here so OTP login doesn't
        silently pick an arbitrary match. Ordered by ensemble then name so a
        disambiguation prompt's numbering is stable across calls.
        """
        return self._execute(
            self._PERSON_MEMBERSHIP_SELECT
            + " WHERE p.phone = %(phone)s ORDER BY e.name, p.name;",
            {'phone': phone}, fetch='all',
        )

    def get_person_with_membership(self, person_id: str) -> Optional[Dict]:
        """
        Same shape as lookup_person_by_phone, keyed by person_id. Used by
        get_current_person on every authenticated request to resolve the
        caller's ensemble/role from their session.
        """
        return self._execute(
            self._PERSON_MEMBERSHIP_SELECT + " WHERE p.id = %(person_id)s LIMIT 1;",
            {'person_id': person_id},
        )

    def get_clinical_record_by_id(self, record_id: str) -> Optional[Dict]:
        return self._execute(
            "SELECT * FROM clinical_records WHERE id = %(id)s;", {'id': record_id}
        )

    # --- AUTH: OTP CODES ---

    def create_otp_code(self, phone: str, code_hash: str, expires_at: datetime) -> Dict:
        return self._execute("""
            INSERT INTO otp_codes (phone, code_hash, expires_at)
            VALUES (%(phone)s, %(code_hash)s, %(expires_at)s)
            RETURNING *;
        """, {'phone': phone, 'code_hash': code_hash, 'expires_at': expires_at})

    def count_recent_otp_requests(self, phone: str, since: datetime) -> int:
        row = self._execute("""
            SELECT COUNT(*) AS n FROM otp_codes
            WHERE phone = %(phone)s AND created_at > %(since)s;
        """, {'phone': phone, 'since': since})
        return row['n'] if row else 0

    def get_latest_unconsumed_otp(self, phone: str) -> Optional[Dict]:
        return self._execute("""
            SELECT * FROM otp_codes
            WHERE phone = %(phone)s AND consumed_at IS NULL AND expires_at > now()
            ORDER BY created_at DESC LIMIT 1;
        """, {'phone': phone})

    def increment_otp_attempts(self, otp_id: str) -> None:
        self._execute(
            "UPDATE otp_codes SET attempts = attempts + 1 WHERE id = %(id)s;",
            {'id': otp_id}, fetch=None,
        )

    def consume_otp_code(self, otp_id: str) -> None:
        self._execute(
            "UPDATE otp_codes SET consumed_at = now() WHERE id = %(id)s;",
            {'id': otp_id}, fetch=None,
        )

    # --- AUTH: SESSIONS ---

    def create_session(self, person_id: str, token_hash: str, expires_at: datetime) -> Dict:
        return self._execute("""
            INSERT INTO sessions (person_id, token_hash, expires_at)
            VALUES (%(person_id)s, %(token_hash)s, %(expires_at)s)
            RETURNING *;
        """, {'person_id': person_id, 'token_hash': token_hash, 'expires_at': expires_at})

    def get_session_by_token_hash(self, token_hash: str) -> Optional[Dict]:
        return self._execute("""
            SELECT * FROM sessions
            WHERE token_hash = %(token_hash)s
              AND revoked_at IS NULL AND expires_at > now();
        """, {'token_hash': token_hash})

    def touch_session(self, session_id: str, new_expires_at: datetime) -> None:
        self._execute("""
            UPDATE sessions SET last_used_at = now(), expires_at = %(expires_at)s
            WHERE id = %(id)s;
        """, {'id': session_id, 'expires_at': new_expires_at}, fetch=None)

    def revoke_session(self, token_hash: str) -> None:
        self._execute(
            "UPDATE sessions SET revoked_at = now() WHERE token_hash = %(token_hash)s;",
            {'token_hash': token_hash}, fetch=None,
        )

    def list_circles_for_person(self, ensemble_id: str, person_id: str,
                                 user_role: str) -> List[Dict]:
        """
        Admins see all circles in the ensemble.
        Members see only circles they belong to via circle_memberships.
        """
        if user_role == 'admin':
            return self._execute("""
                SELECT * FROM care_circles
                WHERE ensemble_id = %(ensemble_id)s
                ORDER BY name;
            """, {'ensemble_id': ensemble_id}, fetch='all')
        else:
            return self._execute("""
                SELECT DISTINCT cc.*
                FROM care_circles cc
                JOIN circle_memberships cm ON cc.id = cm.circle_id
                WHERE cc.ensemble_id = %(ensemble_id)s
                  AND cm.person_id = %(person_id)s
                ORDER BY cc.name;
            """, {'ensemble_id': ensemble_id, 'person_id': person_id}, fetch='all')

    def list_people_for_person(self, ensemble_id: str, person_id: str,
                                user_role: str) -> List[Dict]:
        """
        Admins see all people in the ensemble with their care roles and user roles.
        Members see only people in their own circles.
        One row per person (not per circle_membership) — circle_ids/circle_names/
        care_roles are arrays aggregated in SQL, since a person can now belong
        to more than one circle (inner + outer). Aggregating here means every
        caller gets one row per person for free, rather than needing to
        dedupe client-side.
        """
        if user_role == 'admin':
            return self._execute("""
                SELECT
                    p.id,
                    p.name,
                    p.email,
                    p.phone,
                    p.aliases,
                    p.notes,
                    p.clinical_access,
                    COALESCE(em.user_role, 'member') AS user_role,
                    array_agg(DISTINCT cm.role)      FILTER (WHERE cm.role IS NOT NULL)      AS care_roles,
                    array_agg(DISTINCT cm.circle_id) FILTER (WHERE cm.circle_id IS NOT NULL) AS circle_ids,
                    array_agg(DISTINCT cc.name)      FILTER (WHERE cc.name IS NOT NULL)      AS circle_names
                FROM people p
                LEFT JOIN ensemble_memberships em
                    ON em.person_id = p.id
                   AND em.ensemble_id = %(ensemble_id)s
                LEFT JOIN circle_memberships cm ON cm.person_id = p.id
                    AND cm.circle_id IN (
                        SELECT id FROM care_circles WHERE ensemble_id = %(ensemble_id)s
                    )
                LEFT JOIN care_circles cc ON cc.id = cm.circle_id
                WHERE p.ensemble_id = %(ensemble_id)s
                GROUP BY p.id, p.name, p.email, p.phone, p.aliases, p.notes,
                         p.clinical_access, em.user_role
                ORDER BY p.name;
            """, {'ensemble_id': ensemble_id}, fetch='all')
        else:
            return self._execute("""
                SELECT
                    p.id,
                    p.name,
                    p.email,
                    p.phone,
                    p.aliases,
                    p.notes,
                    p.clinical_access,
                    em_target.user_role,
                    array_agg(DISTINCT cm.role)      FILTER (WHERE cm.role IS NOT NULL)      AS care_roles,
                    array_agg(DISTINCT cm.circle_id) FILTER (WHERE cm.circle_id IS NOT NULL) AS circle_ids,
                    array_agg(DISTINCT cc.name)      FILTER (WHERE cc.name IS NOT NULL)      AS circle_names
                FROM people p
                JOIN circle_memberships cm ON cm.person_id = p.id
                JOIN care_circles cc ON cc.id = cm.circle_id
                                     AND cc.ensemble_id = %(ensemble_id)s
                LEFT JOIN ensemble_memberships em_target
                    ON em_target.person_id = p.id
                   AND em_target.ensemble_id = %(ensemble_id)s
                WHERE cm.circle_id IN (
                    SELECT circle_id FROM circle_memberships
                    WHERE person_id = %(person_id)s
                )
                GROUP BY p.id, p.name, p.email, p.phone, p.aliases, p.notes,
                         p.clinical_access, em_target.user_role
                ORDER BY p.name;
            """, {'ensemble_id': ensemble_id, 'person_id': person_id}, fetch='all')

    def get_ensemble_activity(self, ensemble_id: str, person_id: str,
                               user_role: str, limit: int = 30) -> List[Dict]:
        """
        Recent messages across circles the person can see.
        Admins see all circles; members see only their circles.
        """
        if user_role == 'admin':
            return self._execute("""
                SELECT
                    m.id,
                    m.body          AS message,
                    m.direction,
                    m.sent_at       AS created_at,
                    m.circle_id,
                    cc.name         AS circle_name,
                    COALESCE(p.name, m.raw->>'external_name', 'Take Five') AS sender_name,
                    CASE
                        WHEN m.person_id IS NOT NULL THEN 'human'
                        WHEN m.message_type = 'external_reference' THEN 'external'
                        ELSE 'bot'
                    END AS author_type
                FROM messages m
                JOIN care_circles cc ON cc.id = m.circle_id
                LEFT JOIN people p ON p.id = m.person_id
                WHERE cc.ensemble_id = %(ensemble_id)s
                ORDER BY m.sent_at DESC
                LIMIT %(limit)s;
            """, {'ensemble_id': ensemble_id, 'limit': limit}, fetch='all')
        else:
            return self._execute("""
                SELECT
                    m.id,
                    m.body          AS message,
                    m.direction,
                    m.sent_at       AS created_at,
                    m.circle_id,
                    cc.name         AS circle_name,
                    COALESCE(p.name, m.raw->>'external_name', 'Take Five') AS sender_name,
                    CASE
                        WHEN m.person_id IS NOT NULL THEN 'human'
                        WHEN m.message_type = 'external_reference' THEN 'external'
                        ELSE 'bot'
                    END AS author_type
                FROM messages m
                JOIN care_circles cc ON cc.id = m.circle_id
                LEFT JOIN people p ON p.id = m.person_id
                WHERE cc.ensemble_id = %(ensemble_id)s
                  AND m.circle_id IN (
                      SELECT circle_id FROM circle_memberships
                      WHERE person_id = %(person_id)s
                  )
                ORDER BY m.sent_at DESC
                LIMIT %(limit)s;
            """, {'ensemble_id': ensemble_id, 'person_id': person_id, 'limit': limit}, fetch='all')

    def get_last_digest(self, ensemble_id: str, person_id: Optional[str] = None,
                        user_role: Optional[str] = None) -> Optional[Dict]:
        """
        Return the most recent outbound digest per circle in the ensemble.
        Used by the ensemble admin/member page.

        person_id/user_role: when provided, non-admin members only see
        digests for circles they actually belong to via circle_memberships —
        same pattern as get_ensemble_activity(). Previously this had no
        filtering at all, so an outer circle member would see the inner
        circle's digest too, directly undermining the card #44 boundary.
        Both params optional (default None/no filtering) only for backward
        compatibility with any other existing caller; every caller should
        pass both going forward. Admins always see every circle's digest.
        """
        if user_role and user_role != 'admin' and person_id:
            return self._execute("""
                SELECT DISTINCT ON (cc.id)
                    m.id,
                    m.body,
                    m.sent_at,
                    cc.id   AS circle_id,
                    cc.name AS circle_name
                FROM messages m
                JOIN care_circles cc ON cc.id = m.circle_id
                WHERE cc.ensemble_id = %(ensemble_id)s
                  AND m.direction = 'outbound'
                  AND m.message_type = 'digest'
                  AND cc.id IN (
                      SELECT circle_id FROM circle_memberships
                      WHERE person_id = %(person_id)s
                  )
                ORDER BY cc.id, m.sent_at DESC;
            """, {'ensemble_id': ensemble_id, 'person_id': person_id}, fetch='all')
        return self._execute("""
            SELECT DISTINCT ON (cc.id)
                m.id,
                m.body,
                m.sent_at,
                cc.id   AS circle_id,
                cc.name AS circle_name
            FROM messages m
            JOIN care_circles cc ON cc.id = m.circle_id
            WHERE cc.ensemble_id = %(ensemble_id)s
              AND m.direction = 'outbound'
              AND m.message_type = 'digest'
            ORDER BY cc.id, m.sent_at DESC;
        """, {'ensemble_id': ensemble_id}, fetch='all')

    def get_clinical_records_for_ensemble(
        self, ensemble_id: str, resource_type: Optional[str] = None
    ) -> List[Dict]:
        """
        Return clinical records for all seniors in the ensemble.
        Optionally filtered by resource_type (e.g. 'MedicationStatement', 'CareTeamMember').
        Used by the ensemble admin Health panel.
        """
        base = """
            SELECT
                cr.id,
                cr.person_id,
                cr.resource_type,
                cr.data,
                cr.notes,
                cr.status,
                cr.created_at,
                cr.updated_at,
                p.name AS person_name
            FROM clinical_records cr
            JOIN people p ON p.id = cr.person_id
            JOIN circle_memberships cm ON cm.person_id = p.id
            JOIN care_circles cc ON cc.id = cm.circle_id
            WHERE cc.ensemble_id = %(ensemble_id)s
              AND cm.role = 'senior'
        """
        params = {'ensemble_id': ensemble_id}
        if resource_type:
            base += " AND cr.resource_type = %(resource_type)s"
            params['resource_type'] = resource_type
        base += " ORDER BY p.name, cr.resource_type, cr.created_at;"
        return self._execute(base, params, fetch='all')

    def invite_person_to_ensemble(
        self,
        ensemble_id: str,
        circle_id: str,
        name: str,
        email: str,
        phone: Optional[str],
        care_role: str,
        user_role: str,
        clinical_access: bool = False,
    ) -> Dict:
        """
        Idempotent invite: if a person with this email already exists in the
        ensemble, update their memberships rather than creating a duplicate.
        Returns the person row.

        clinical_access: decided here, at invite time, independent of
        care_role/circle_id — being invited to the inner circle does not by
        itself grant clinical access, and being invited to an outer circle
        does not by itself deny it (e.g. an outer-circle-only invitee with
        real clinical authority). Caller passes the correct value explicitly;
        default False if omitted. Only applied on first creation — an
        existing person's clinical_access is left untouched by re-inviting
        them to a different circle.
        """
        conn = self._pool.getconn()
        try:
            with conn:
                with conn.cursor() as cur:

                    # 1. Check for existing person with this email in the ensemble
                    cur.execute("""
                        SELECT id FROM people
                        WHERE ensemble_id = %(ensemble_id)s
                          AND LOWER(email) = LOWER(%(email)s)
                        LIMIT 1;
                    """, {'ensemble_id': ensemble_id, 'email': email})
                    existing = cur.fetchone()

                    if existing:
                        person_id = existing['id']
                        # Update phone if provided
                        if phone:
                            cur.execute("""
                                UPDATE people SET phone = %(phone)s
                                WHERE id = %(id)s;
                            """, {'phone': phone, 'id': person_id})
                    else:
                        # 2. Create the person
                        cur.execute("""
                            INSERT INTO people (ensemble_id, name, email, phone, clinical_access)
                            VALUES (%(ensemble_id)s, %(name)s, %(email)s, %(phone)s, %(clinical_access)s)
                            RETURNING *;
                        """, {
                            'ensemble_id': ensemble_id,
                            'name':        name,
                            'email':       email,
                            'phone':       phone,
                            'clinical_access': clinical_access,
                        })
                        person_id = cur.fetchone()['id']

                    # 3. Upsert ensemble membership (user role)
                    cur.execute("""
                        INSERT INTO ensemble_memberships (ensemble_id, person_id, user_role)
                        VALUES (%(ensemble_id)s, %(person_id)s, %(user_role)s)
                        ON CONFLICT (ensemble_id, person_id) DO UPDATE
                            SET user_role = EXCLUDED.user_role;
                    """, {'ensemble_id': ensemble_id, 'person_id': person_id, 'user_role': user_role})

                    # 4. Upsert circle membership (care role)
                    cur.execute("""
                        INSERT INTO circle_memberships (circle_id, person_id, role)
                        VALUES (%(circle_id)s, %(person_id)s, %(role)s)
                        ON CONFLICT (circle_id, person_id) DO UPDATE
                            SET role = EXCLUDED.role;
                    """, {'circle_id': circle_id, 'person_id': person_id, 'role': care_role})

                    # 5. Return full person row
                    cur.execute("SELECT * FROM people WHERE id = %(id)s;", {'id': person_id})
                    person = cur.fetchone()

                    return person
        finally:
            self._pool.putconn(conn)

    def upsert_ensemble_membership(self, ensemble_id: str, person_id: str, user_role: str) -> Dict:
        """Set or update a person's user role in an ensemble."""
        return self._execute("""
            INSERT INTO ensemble_memberships (ensemble_id, person_id, user_role)
            VALUES (%(ensemble_id)s, %(person_id)s, %(user_role)s)
            ON CONFLICT (ensemble_id, person_id) DO UPDATE SET user_role = EXCLUDED.user_role
            RETURNING *;
        """, {'ensemble_id': ensemble_id, 'person_id': person_id, 'user_role': user_role})

    def get_medications_for_ensemble(self, ensemble_id: str) -> List[Dict]:
        """
        Return active MedicationStatements for all seniors in the ensemble.
        Used by the ensemble admin/member overview panel.
        """
        return self._execute("""
            SELECT
                p.name          AS person_name,
                cr.data,
                cr.created_at
            FROM clinical_records cr
            JOIN people p ON p.id = cr.person_id
            JOIN circle_memberships cm ON cm.person_id = p.id
            JOIN care_circles cc ON cc.id = cm.circle_id
            WHERE cc.ensemble_id = %(ensemble_id)s
              AND cm.role = 'senior'
              AND cr.resource_type = 'MedicationStatement'
              AND cr.status = 'active'
            ORDER BY p.name, cr.created_at;
        """, {'ensemble_id': ensemble_id}, fetch='all')

    def get_digest_history(self, ensemble_id: str, limit: int = 20,
                           person_id: Optional[str] = None,
                           user_role: Optional[str] = None) -> List[Dict]:
        """
        Return all digests for the ensemble, newest first.
        Used by the digest history panel.

        person_id/user_role: same circle-scoping as get_last_digest() —
        non-admin members only see digests for circles they belong to.
        Previously unfiltered entirely; see get_last_digest()'s docstring.
        """
        if user_role and user_role != 'admin' and person_id:
            return self._execute("""
                SELECT
                    m.id,
                    m.body,
                    m.sent_at,
                    cc.id   AS circle_id,
                    cc.name AS circle_name
                FROM messages m
                JOIN care_circles cc ON cc.id = m.circle_id
                WHERE cc.ensemble_id = %(ensemble_id)s
                  AND m.direction = 'outbound'
                  AND m.message_type = 'digest'
                  AND cc.id IN (
                      SELECT circle_id FROM circle_memberships
                      WHERE person_id = %(person_id)s
                  )
                ORDER BY m.sent_at DESC
                LIMIT %(limit)s;
            """, {'ensemble_id': ensemble_id, 'person_id': person_id, 'limit': limit}, fetch='all')
        return self._execute("""
            SELECT
                m.id,
                m.body,
                m.sent_at,
                cc.id   AS circle_id,
                cc.name AS circle_name
            FROM messages m
            JOIN care_circles cc ON cc.id = m.circle_id
            WHERE cc.ensemble_id = %(ensemble_id)s
              AND m.direction = 'outbound'
              AND m.message_type = 'digest'
            ORDER BY m.sent_at DESC
            LIMIT %(limit)s;
        """, {'ensemble_id': ensemble_id, 'limit': limit}, fetch='all')


# Module-level singleton — import this instead of instantiating directly.
repo = TakeFiveRepository()
