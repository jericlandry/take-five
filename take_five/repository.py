import json
import os
import re
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional

from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor, Json

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

    def _execute(self, query: str, params: tuple = (), fetch: str = 'one'):
        with psycopg2.connect(**self.db_config, cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                conn.commit()
                if fetch == 'one': return cur.fetchone()
                if fetch == 'all': return cur.fetchall()
                return None

    # --- PEOPLE ---

    def get_person_by_external_id(self, external_id: str) -> Optional[Dict]:
        return self._execute("SELECT * FROM people WHERE external_id = %s;", (str(external_id),))

    def get_person_by_id(self, person_id: str) -> Optional[Dict]:
        return self._execute("SELECT * FROM people WHERE id = %s;", (person_id,))

    def update_person(self, person_id: str, name: Optional[str] = None,
                        phone: Optional[str] = None, email: Optional[str] = None,
                        aliases: Optional[List[str]] = None, notes: Optional[str] = None,
                        external_id: Optional[str] = None, date_of_birth: Optional[str] = None) -> Dict:
        query = """
            UPDATE people SET
                name          = COALESCE(%(name)s, name),
                phone         = COALESCE(%(phone)s, phone),
                email         = COALESCE(%(email)s, email),
                aliases       = COALESCE(%(aliases)s, aliases),
                notes         = COALESCE(%(notes)s, notes),
                external_id   = COALESCE(%(external_id)s, external_id),
                date_of_birth = %(date_of_birth)s
            WHERE id = %(id)s
            RETURNING *;
        """
        return self._execute(query, {
            'id': person_id, 'name': name,
            'phone': phone, 'email': email,
            'aliases': aliases, 'notes': notes,
            'external_id': external_id,
            'date_of_birth': date_of_birth,
        })

    def add_person_to_ensemble(self, ensemble_id: str, name: str, **kwargs) -> Dict:
        query = """
            INSERT INTO people (ensemble_id, name, phone, email, timezone, aliases, notes, external_id, date_of_birth)
            VALUES (%(ensemble_id)s, %(name)s, %(phone)s, %(email)s, %(tz)s, %(aliases)s, %(notes)s, %(external_id)s, %(dob)s)
            RETURNING *;
        """
        return self._execute(query, {
            'ensemble_id': ensemble_id, 'name': name,
            'phone': kwargs.get('phone'), 'email': kwargs.get('email'),
            'tz': kwargs.get('timezone', 'America/Chicago'),
            'aliases': kwargs.get('aliases', []), 'notes': kwargs.get('notes'),
            'external_id': kwargs.get('external_id'),
            'dob': kwargs.get('date_of_birth'),
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
                           external_id: Optional[str] = None) -> Dict:
        query = """
            INSERT INTO care_circles (ensemble_id, name, status, external_id)
            VALUES (%(ensemble_id)s, %(name)s, %(status)s, %(external_id)s)
            RETURNING *;
        """
        return self._execute(query, {
            'ensemble_id': ensemble_id, 'name': name,
            'status': status, 'external_id': external_id
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

    def get_circle_by_twilio_number(self, twilio_number: str) -> Optional[Dict]:
        """Look up a care circle by its dedicated Twilio SMS number."""
        return self._execute(
            "SELECT * FROM care_circles WHERE integration_config->>'twilio_number' = %s;",
            (twilio_number,)
        )

    def find_caregiver_by_phone_and_circle(self, phone: str, circle_id: str) -> Optional[Dict]:
        """Find an sms_active circle member by their phone number, scoped to a specific circle."""
        return self._execute("""
            SELECT p.*, cm.role, cm.sms_active
            FROM people p
            JOIN circle_memberships cm ON p.id = cm.person_id
            WHERE p.phone = %(phone)s
              AND cm.circle_id = %(circle_id)s
              AND cm.sms_active = true;
        """, {'phone': phone, 'circle_id': circle_id})

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
                cm.role         AS person_role,
                c.name          AS circle_name,
                COUNT(m.id)     AS msg_count,
                MAX(m.sent_at)  AS last_active
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
                     cm.role, c.name
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

    def remove_person_from_circle(self, circle_id: str, person_id: str) -> None:
        self._execute("""
            DELETE FROM circle_memberships
            WHERE circle_id = %(circle_id)s AND person_id = %(person_id)s;
        """, {'circle_id': circle_id, 'person_id': person_id}, fetch='all')

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

    def get_messages(self, circle_id: str, start_date: datetime = None,
                     end_date: datetime = None, limit: int = None) -> List[Dict]:
        """
        Fetch messages for a circle. Bot messages (person_id IS NULL) are
        labelled 'Take Five' so ask() can identify them in context.
        """
        query = """
            SELECT
                m.*,
                COALESCE(p.name, 'Take Five') AS author_name
            FROM messages m
            LEFT JOIN people p ON m.person_id = p.id
            WHERE m.circle_id = %s
        """
        params = [str(circle_id)]

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

    def fetch_semantic_chunks(self, circle_id: str, question_embedding: list[float],
                               limit: int = 10) -> list:
        query = """
            SELECT
                mc.body,
                mc.context_header,
                mc.context_summary,
                mc.sent_at,
                1 - (mc.embedding <=> %(embedding)s::vector) AS similarity
            FROM message_chunks mc
            JOIN care_circles c ON mc.circle_id = c.id
            WHERE c.id = %(circle_id)s
            ORDER BY mc.embedding <=> %(embedding)s::vector
            LIMIT %(limit)s
        """
        return self._execute(
            query,
            {"embedding": str(question_embedding), "circle_id": circle_id, "limit": limit},
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

    def get_pending_corroboration_signals(self, circle_id: str, max_age_days: int = 7) -> List[Dict]:
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
        """
        query = """
            SELECT cs.*, p.name AS subject_name
            FROM clinical_signals cs
            LEFT JOIN people p ON p.id = cs.subject_id
            WHERE cs.circle_id = %(circle_id)s
              AND cs.request_corroboration = true
              AND cs.corroboration_requested_at IS NULL
              AND cs.detected_at >= now() - make_interval(days => %(max_age_days)s)
            ORDER BY cs.detected_at ASC;
        """
        return self._execute(
            query,
            {"circle_id": str(circle_id), "max_age_days": max_age_days},
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

        with psycopg2.connect(**self.db_config, cursor_factory=RealDictCursor) as conn:
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

                conn.commit()
                return record

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

        with psycopg2.connect(**self.db_config, cursor_factory=RealDictCursor) as conn:
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

                conn.commit()
                return record

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

        If person_id is provided, scopes to that one senior only (e.g. for a
        prep packet targeted at a single senior in a circle with multiple
        seniors). Otherwise returns records for every senior in the circle.
        """
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

    def get_prep_packets(self, circle_id: str, limit: int = 20) -> List[Dict]:
        """
        Return prep packets for a circle, newest first.
        These are outbound messages with message_type='prep_packet'.
        Metadata (doctor, appointment, lookback) lives in the raw JSONB column.
        """
        return self._execute("""
            SELECT
                m.id,
                m.body,
                m.sent_at,
                m.raw
            FROM messages m
            WHERE m.circle_id = %(circle_id)s
              AND m.message_type = 'prep_packet'
            ORDER BY m.sent_at DESC
            LIMIT %(limit)s;
        """, {'circle_id': circle_id, 'limit': limit}, fetch='all')

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
                p.date_of_birth,
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

    def lookup_person_by_email(self, email: str) -> Optional[Dict]:
        """
        Look up a person by email and return their ensemble membership context.
        Used by /auth/lookup to authenticate the ensemble admin/member page.
        Returns person + ensemble + user_role, or None if not found.
        """
        return self._execute("""
            SELECT
                p.id            AS person_id,
                p.name          AS person_name,
                p.email,
                p.phone,
                p.aliases,
                p.notes,
                p.date_of_birth,
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
            WHERE LOWER(p.email) = LOWER(%(email)s)
            LIMIT 1;
        """, {'email': email})

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
        Includes care_role (from circle_memberships) and user_role
        (from ensemble_memberships), plus which circle(s) they belong to.
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
                    COALESCE(em.user_role, 'member')    AS user_role,
                    cm.role                             AS care_role,
                    cm.circle_id,
                    cc.name                             AS circle_name
                FROM people p
                LEFT JOIN ensemble_memberships em
                    ON em.person_id = p.id
                   AND em.ensemble_id = %(ensemble_id)s
                LEFT JOIN circle_memberships cm ON cm.person_id = p.id
                LEFT JOIN care_circles cc ON cc.id = cm.circle_id
                                         AND cc.ensemble_id = %(ensemble_id)s
                WHERE p.ensemble_id = %(ensemble_id)s
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
                    em_target.user_role,
                    cm.role         AS care_role,
                    cm.circle_id,
                    cc.name         AS circle_name
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
                    COALESCE(p.name, 'Take Five') AS sender_name,
                    CASE WHEN m.person_id IS NULL THEN 'bot' ELSE 'human' END AS author_type
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
                    COALESCE(p.name, 'Take Five') AS sender_name,
                    CASE WHEN m.person_id IS NULL THEN 'bot' ELSE 'human' END AS author_type
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

    def get_last_digest(self, ensemble_id: str) -> Optional[Dict]:
        """
        Return the most recent outbound digest per circle in the ensemble.
        Used by the ensemble admin/member page.
        """
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
    ) -> Dict:
        """
        Idempotent invite: if a person with this email already exists in the
        ensemble, update their memberships rather than creating a duplicate.
        Returns the person row.
        """
        with psycopg2.connect(**self.db_config, cursor_factory=RealDictCursor) as conn:
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
                        INSERT INTO people (ensemble_id, name, email, phone)
                        VALUES (%(ensemble_id)s, %(name)s, %(email)s, %(phone)s)
                        RETURNING *;
                    """, {
                        'ensemble_id': ensemble_id,
                        'name':        name,
                        'email':       email,
                        'phone':       phone,
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

                conn.commit()
                return person

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

    def get_digest_history(self, ensemble_id: str, limit: int = 20) -> List[Dict]:
        """
        Return all digests for the ensemble, newest first.
        Used by the digest history panel.
        """
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
