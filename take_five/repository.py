import psycopg2
from psycopg2.extras import RealDictCursor, Json
from typing import List, Dict, Optional
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()  # Load environment variables from .env file

class TakeFiveRepository:
    def __init__(self):
        self.db_config = {
                'dbname': 'takefive',
                'user': os.getenv('DB_USER'),
                'password': os.getenv('DB_PASSWORD'),
                'host': 'dpg-d78po2h5pdvs73b7l7rg-a.virginia-postgres.render.com',
                'port': 5432
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
        """Creates or updates a person. p_type: senior, family, aide, nurse, agent"""
        query = """
            INSERT INTO people (external_id, external_type, name, type, email, phone, timezone)
            VALUES (%(ext_id)s, 'groupme', %(name)s, %(type)s, %(email)s, %(phone)s, %(tz)s)
            ON CONFLICT (external_id) 
            DO UPDATE SET name = EXCLUDED.name, type = EXCLUDED.type
            RETURNING *;
        """
        params = {
            'ext_id': str(external_id), 'name': name, 'type': p_type,
            'email': kwargs.get('email'), 'phone': kwargs.get('phone'),
            'tz': kwargs.get('timezone', 'America/Chicago')
        }
        return self._execute(query, params)

    def get_person_by_external_id(self, external_id: str) -> Optional[Dict]:
        return self._execute("SELECT * FROM people WHERE external_id = %s;", (str(external_id),))
    
    def find_person_by_phone(self, phone: str) -> Optional[Dict]:
        """Finds a person by their phone number."""
        query = """
            SELECT * FROM people 
            WHERE phone = %(phone)s 
            LIMIT 1;
        """
        params = {'phone': phone}
        return self._execute(query, params)
    
    def add_person_to_ensemble(self, ensemble_id: str, name: str, p_type: str, **kwargs) -> Dict:
        """Creates a person and associates them with an ensemble."""
        query = """
            INSERT INTO people (ensemble_id, name, type, phone, email, timezone, aliases, notes)
            VALUES (%(ensemble_id)s, %(name)s, %(type)s, %(phone)s, %(email)s, %(tz)s, %(aliases)s, %(notes)s)
            RETURNING *;
        """
        params = {
            'ensemble_id': ensemble_id,
            'name': name,
            'type': p_type,
            'phone': kwargs.get('phone'),
            'email': kwargs.get('email'),
            'tz': kwargs.get('timezone', 'America/Chicago'),
            'aliases': kwargs.get('aliases', []),   # default to empty list not None
            'notes': kwargs.get('notes')            # string, None is fine
        }
        return self._execute(query, params)

    # --- CARE CIRCLES ---
    def upsert_circle(self, external_id: str, name: str) -> Dict:
        """Creates or updates a circle based on GroupMe Group ID."""
        query = """
            INSERT INTO care_circles (external_id, external_type, name)
            VALUES (%s, 'groupme', %s)
            ON CONFLICT (external_id) 
            DO UPDATE SET name = EXCLUDED.name
            RETURNING *;
        """
        return self._execute(query, (str(external_id), name))
    
    def create_care_circle(self, ensemble_id: str, name: str, status: str = 'active',
                        external_id: Optional[str] = None, external_type: str = 'groupme') -> Dict:
        """Creates a care circle under an ensemble."""
        query = """
            INSERT INTO care_circles (ensemble_id, name, status, external_id, external_type)
            VALUES (%(ensemble_id)s, %(name)s, %(status)s, %(external_id)s, %(external_type)s)
            RETURNING *;
        """
        return self._execute(query, {
            'ensemble_id': ensemble_id,
            'name': name,
            'status': status,
            'external_id': external_id,
            'external_type': external_type
        })

    def get_circle_by_external_id(self, external_id: str) -> Optional[Dict]:
        return self._execute("SELECT * FROM care_circles WHERE external_id = %s;", (str(external_id),))
    
    def get_circle_by_id(self, circle_id: str) -> Optional[Dict]:
        return self._execute("SELECT * FROM care_circles WHERE id = %s;", (str(circle_id),))

    def find_circles_by_person(self, person_external_id: str) -> List[Dict]:
        """Returns all circles associated with a specific person's external_id."""
        query = """
            SELECT c.*, m.role 
            FROM care_circles c
            JOIN circle_memberships m ON c.id = m.circle_id
            JOIN people p ON m.person_id = p.id
            WHERE p.external_id = %(ext_id)s;
        """
        params = {'ext_id': str(person_external_id)}
        return self._execute(query, params)

    def fetch_circle_roster(self, circle_id: str) -> list:
        """Fetch all circle members with aliases, notes, and role."""
        query = """
            SELECT
                c.name    AS circle_name,
                p.name    AS member_name,
                p.aliases AS person_aliases,
                p.notes   AS person_notes,
                cm.role   AS person_role
            FROM care_circles c
            JOIN circle_memberships cm ON c.id = cm.circle_id
            JOIN people p ON cm.person_id = p.id
            WHERE c.id = %(circle_id)s
            ORDER BY cm.role, p.name
        """
        return self._execute(query, {"circle_id": circle_id}, fetch="all")

    # --- MEMBERSHIPS ---
    def list_care_circles(self, ensemble_id: str) -> List[Dict]:
        """Returns all care circles belonging to an ensemble."""
        return self._execute(
            "SELECT * FROM care_circles WHERE ensemble_id = %s ORDER BY name;",
            (ensemble_id,),
            fetch='all'
        )
    
    def add_person_to_circle(self, circle_id: str, person_id: str, role: str) -> Dict:
        """Adds a person to a care circle with a role."""
        query = """
            INSERT INTO circle_memberships (circle_id, person_id, role)
            VALUES (%(circle_id)s, %(person_id)s, %(role)s)
            ON CONFLICT (circle_id, person_id) DO UPDATE SET role = EXCLUDED.role
            RETURNING *;
        """
        return self._execute(query, {
            'circle_id': circle_id,
            'person_id': person_id,
            'role': role
        })

    def add_to_circle(self, circle_ext_id: str, person_ext_id: str, role: str) -> Dict:
        """Links person to circle using external IDs."""
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
                    direction: str = 'inbound', raw_data: Optional[Dict] = None, channel: str = 'groupme') -> Dict:
        """
        Logs a message. person_ext_id can be None for system/agent notes.
        parsed_data should be a Python dict (mood_score, meds_taken, etc.)
        """
        query = """
            INSERT INTO messages (circle_id, person_id, message_type, direction, body, raw, channel)
            VALUES (
                (SELECT id FROM care_circles WHERE external_id = %s),
                (SELECT id FROM people WHERE external_id = %s),
                %s, %s, %s, %s, %s
            ) RETURNING *;
        """
        return self._execute(query, (
            str(circle_ext_id), 
            str(person_ext_id) if person_ext_id else None, 
            msg_type, direction, body, 
            Json(raw_data) if raw_data else None,
            channel
        ))

    def get_recent_messages(self, circle_id: str) -> List[Dict]:
        query = """
            SELECT m.*, p.name as author_name 
            FROM messages m
            LEFT JOIN people p ON m.person_id = p.id
            WHERE m.circle_id = (SELECT id FROM care_circles WHERE id = %s)
            ORDER BY m.sent_at DESC;
        """
        # Fix: Add a comma after the ID to make it a tuple: (value,)
        return self._execute(query, (str(circle_id),), fetch='all')


    def get_messages_in_date_range(
        self, 
        circle_ext_id: str, 
        start_date: datetime, 
        end_date: datetime, 
        limit: int = 100
    ) -> List[Dict]:
        query = """
            SELECT m.*, p.name as author_name 
            FROM messages m
            LEFT JOIN people p ON m.person_id = p.id
            WHERE m.circle_id = (SELECT id FROM care_circles WHERE external_id = %s)
            AND m.sent_at >= %s 
            AND m.sent_at <= %s
            ORDER BY m.sent_at DESC 
            LIMIT %s;
        """
        return self._execute(
            query, 
            (str(circle_ext_id), start_date, end_date, limit), 
            fetch='all'
        )
    
    def upsert_message_chunk(
        self,
        message_id: str,
        circle_id: str,
        chunk_index: int,
        body: str,
        context_header: str,
        context_summary: str,
        embedded_text: str,
        embedding: list,
        sent_at
    ) -> Dict:
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
    
    def fetch_semantic_chunks(
        self,
        circle_id: str,
        question_embedding: list[float],
        limit: int=10,
    ) -> list:
        """Retrieve top-k message chunks by cosine similarity."""
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
    
    # --- ENSEMBLES ---
    def create_ensemble(self, name: str, plan: str = 'family_plus', status: str = 'trial') -> Dict:
        """Creates a new ensemble (family account)."""
        query = """
            INSERT INTO ensembles (name, plan, status)
            VALUES (%(name)s, %(plan)s, %(status)s)
            RETURNING *;
        """
        params = {'name': name, 'plan': plan, 'status': status}
        return self._execute(query, params)

    def get_ensemble(self, ensemble_id: str) -> Optional[Dict]:
        """Fetch an ensemble by id."""
        return self._execute(
            "SELECT * FROM ensembles WHERE id = %s;", 
            (ensemble_id,)
        )

    def get_ensemble_by_name(self, name: str) -> Optional[Dict]:
        """Fetch an ensemble by name."""
        return self._execute(
            "SELECT * FROM ensembles WHERE name = %s;", 
            (name,)
        )

    def list_ensembles(self) -> List[Dict]:
        """Returns all ensembles."""
        return self._execute(
            "SELECT * FROM ensembles ORDER BY created_at DESC;", 
            fetch='all'
        )

    def list_people_by_ensemble(self, ensemble_id: str) -> List[Dict]:
        """Returns all people belonging to an ensemble."""
        return self._execute(
            "SELECT * FROM people WHERE ensemble_id = %s ORDER BY name;",
            (ensemble_id,),
            fetch='all'
        )