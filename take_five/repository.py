import psycopg2
from psycopg2.extras import RealDictCursor, Json
from typing import List, Dict, Optional
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

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

    def upsert_person(self, external_id: str, name: str, p_type: str, **kwargs) -> Dict:
        query = """
            INSERT INTO people (external_id, name, type, email, phone, timezone)
            VALUES (%(ext_id)s, %(name)s, %(type)s, %(email)s, %(phone)s, %(tz)s)
            ON CONFLICT (external_id)
            DO UPDATE SET name = EXCLUDED.name, type = EXCLUDED.type
            RETURNING *;
        """
        return self._execute(query, {
            'ext_id': str(external_id), 'name': name, 'type': p_type,
            'email': kwargs.get('email'), 'phone': kwargs.get('phone'),
            'tz': kwargs.get('timezone', 'America/Chicago')
        })

    def get_person_by_external_id(self, external_id: str) -> Optional[Dict]:
        return self._execute("SELECT * FROM people WHERE external_id = %s;", (str(external_id),))

    def get_person_by_id(self, person_id: str) -> Optional[Dict]:
        return self._execute("SELECT * FROM people WHERE id = %s;", (person_id,))

    def find_person_by_phone(self, phone: str) -> Optional[Dict]:
        return self._execute(
            "SELECT * FROM people WHERE phone = %(phone)s LIMIT 1;",
            {'phone': phone}
        )

    def update_person(self, person_id: str, updates) -> Dict:
        query = """
            UPDATE people SET
                name        = COALESCE(%(name)s, name),
                type        = COALESCE(%(type)s, type),
                phone       = COALESCE(%(phone)s, phone),
                email       = COALESCE(%(email)s, email),
                aliases     = COALESCE(%(aliases)s, aliases),
                notes       = COALESCE(%(notes)s, notes),
                external_id = COALESCE(%(external_id)s, external_id)
            WHERE id = %(id)s
            RETURNING *;
        """
        return self._execute(query, {
            'id': person_id, 'name': updates.name, 'type': updates.p_type,
            'phone': updates.phone, 'email': updates.email,
            'aliases': updates.aliases, 'notes': updates.notes,
            'external_id': updates.external_id,
        })

    def add_person_to_ensemble(self, ensemble_id: str, name: str, p_type: str, **kwargs) -> Dict:
        query = """
            INSERT INTO people (ensemble_id, name, type, phone, email, timezone, aliases, notes, external_id)
            VALUES (%(ensemble_id)s, %(name)s, %(type)s, %(phone)s, %(email)s, %(tz)s, %(aliases)s, %(notes)s, %(external_id)s)
            RETURNING *;
        """
        return self._execute(query, {
            'ensemble_id': ensemble_id, 'name': name, 'type': p_type,
            'phone': kwargs.get('phone'), 'email': kwargs.get('email'),
            'tz': kwargs.get('timezone', 'America/Chicago'),
            'aliases': kwargs.get('aliases', []), 'notes': kwargs.get('notes'),
            'external_id': kwargs.get('external_id'),
        })

    # --- CARE CIRCLES ---

    def upsert_circle(self, external_id: str, name: str) -> Dict:
        query = """
            INSERT INTO care_circles (external_id, name)
            VALUES (%s, %s)
            ON CONFLICT (external_id) DO UPDATE SET name = EXCLUDED.name
            RETURNING *;
        """
        return self._execute(query, (str(external_id), name))

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

    def get_circle_by_id(self, circle_id: str) -> Optional[Dict]:
        return self._execute(
            "SELECT * FROM care_circles WHERE id = %s;", (str(circle_id),)
        )

    def find_circles_by_person(self, person_external_id: str) -> List[Dict]:
        query = """
            SELECT c.*, m.role
            FROM care_circles c
            JOIN circle_memberships m ON c.id = m.circle_id
            JOIN people p ON m.person_id = p.id
            WHERE p.external_id = %(ext_id)s;
        """
        return self._execute(query, {'ext_id': str(person_external_id)}, fetch='all')

    def fetch_circle_roster(self, circle_id: str) -> list:
        query = """
            SELECT
                p.id,
                p.name          AS member_name,
                p.type          AS p_type,
                p.phone,
                p.email,
                p.aliases       AS person_aliases,
                p.notes         AS person_notes,
                p.external_id,
                cm.role         AS person_role,
                c.name          AS circle_name
            FROM care_circles c
            JOIN circle_memberships cm ON c.id = cm.circle_id
            JOIN people p ON cm.person_id = p.id
            WHERE c.id = %(circle_id)s
            ORDER BY cm.role, p.name
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

    def add_to_circle(self, circle_ext_id: str, person_ext_id: str, role: str) -> Dict:
        query = """
            INSERT INTO circle_memberships (circle_id, person_id, role)
            VALUES (
                (SELECT id FROM care_circles WHERE external_id = %s),
                (SELECT id FROM people WHERE external_id = %s),
                %s
            )
            ON CONFLICT (circle_id, person_id) DO UPDATE SET role = EXCLUDED.role
            RETURNING *;
        """
        return self._execute(query, (str(circle_ext_id), str(person_ext_id), role))

    # --- MESSAGES ---

    def log_message(self, circle_ext_id: str, person_ext_id: Optional[str],
                    body: str, msg_type: str = 'inbound',
                    direction: str = 'inbound', raw_data: Optional[Dict] = None,
                    channel: str = 'groupme') -> Dict:
        """
        Logs a message to the messages table.

        person_ext_id=None for bot/agent outbound messages — person_id is
        inserted as NULL directly rather than via subquery.

        Semantics:
          direction='inbound',  person_id=<uuid> → human message
          direction='outbound', person_id=NULL   → bot/agent message
        """
        if person_ext_id:
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
            WHERE m.circle_id = (SELECT id FROM care_circles WHERE id = %s)
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
        Insert a clinical record. circle_id is optional provenance — pass it
        when the record originates from a specific chat, omit for admin entry.

        resource_type: 'MedicationStatement' | 'Condition' | 'Observation'
                       'Appointment' | 'AllergyIntolerance' | 'Procedure'
                       'CareTeamMember'
        """
        query = """
            INSERT INTO clinical_records (
                person_id, resource_type, status,
                data, notes, confirmed_by, confirmed_at, source_message_id, circle_id
            ) VALUES (
                %(person_id)s, %(resource_type)s, %(status)s,
                %(data)s, %(notes)s, %(confirmed_by)s,
                %(confirmed_at)s, %(source_message_id)s, %(circle_id)s
            ) RETURNING *;
        """
        return self._execute(query, {
            'person_id':         person_id,
            'resource_type':     resource_type,
            'status':            status,
            'data':              Json(data),
            'notes':             notes,
            'confirmed_by':      confirmed_by,
            'confirmed_at':      datetime.utcnow() if confirmed_by else None,
            'source_message_id': source_message_id,
            'circle_id':         circle_id,
        })

    def update_clinical_record(
        self,
        record_id: str,
        data: Optional[Dict] = None,
        notes: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict:
        """Update an existing clinical record's data, notes, or status."""
        query = """
            UPDATE clinical_records SET
                data   = COALESCE(%(data)s,   data),
                notes  = COALESCE(%(notes)s,  notes),
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
    ) -> List[Dict]:
        """
        Fetch clinical records for all seniors in a circle.
        Resolves seniors via circle_memberships — does not filter by circle_id
        on the clinical_records table.
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

        query += " ORDER BY p.name, cr.created_at DESC"
        return self._execute(query, params, fetch='all')

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
        return self._execute(
            "SELECT * FROM people WHERE ensemble_id = %s ORDER BY name;",
            (ensemble_id,), fetch='all'
        )
