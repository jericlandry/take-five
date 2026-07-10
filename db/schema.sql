--
-- PostgreSQL database dump
--

\restrict S7875gJsJqccfS1w7XCyj6BHxE8uGpsxz7JaR0PPHPD413dHJSsQ7yKCRhKNDgN

-- Dumped from database version 18.3 (Debian 18.3-1.pgdg12+1)
-- Dumped by pg_dump version 18.3

-- Started on 2026-07-07 19:38:39 CDT

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- TOC entry 6 (class 2615 OID 2200)
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

-- *not* creating schema, since initdb creates it


--
-- TOC entry 2 (class 3079 OID 16525)
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- TOC entry 3773 (class 0 OID 0)
-- Dependencies: 2
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- TOC entry 356 (class 1255 OID 17145)
-- Name: set_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- TOC entry 221 (class 1259 OID 16432)
-- Name: care_circles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.care_circles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    external_id text,
    ensemble_id uuid NOT NULL,
    integration_config jsonb DEFAULT '{}'::jsonb,
    CONSTRAINT care_circles_status_check CHECK ((status = ANY (ARRAY['active'::text, 'paused'::text, 'archived'::text])))
);


--
-- TOC entry 222 (class 1259 OID 16453)
-- Name: circle_memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.circle_memberships (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    circle_id uuid NOT NULL,
    person_id uuid NOT NULL,
    role text NOT NULL,
    sms_active boolean DEFAULT true NOT NULL,
    joined_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- TOC entry 227 (class 1259 OID 17187)
-- Name: clinical_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.clinical_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    record_id uuid NOT NULL,
    event_type text NOT NULL,
    changed_fields jsonb,
    previous_values jsonb,
    notes text,
    confirmed_by uuid,
    confirmed_at timestamp with time zone,
    source_message_id uuid,
    created_at timestamp with time zone DEFAULT now()
);


--
-- TOC entry 226 (class 1259 OID 17099)
-- Name: clinical_records; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.clinical_records (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    circle_id uuid,
    person_id uuid NOT NULL,
    resource_type text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    data jsonb DEFAULT '{}'::jsonb NOT NULL,
    fhir_resource jsonb,
    notes text,
    source_message_id uuid,
    confirmed_by uuid,
    confirmed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT clinical_records_resource_type_check CHECK ((resource_type = ANY (ARRAY['MedicationStatement'::text, 'Condition'::text, 'Observation'::text, 'Appointment'::text, 'AllergyIntolerance'::text, 'Procedure'::text, 'CareTeamMember'::text]))),
    CONSTRAINT clinical_records_status_check CHECK ((status = ANY (ARRAY['active'::text, 'discontinued'::text, 'as_needed'::text, 'resolved'::text, 'cancelled'::text])))
);


--
-- TOC entry 229 (class 1259 OID 24737)
-- Name: clinical_signals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.clinical_signals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    message_id uuid NOT NULL,
    circle_id uuid NOT NULL,
    subject_id uuid,
    signal_category character varying(50) NOT NULL,
    signal_type character varying(100) NOT NULL,
    raw_excerpt text,
    mention_style character varying(20),
    confidence double precision,
    channel character varying(20) DEFAULT 'groupme'::character varying,
    request_corroboration boolean DEFAULT false,
    corroboration_requested_at timestamp with time zone,
    superseded_by_id uuid,
    detected_at timestamp with time zone DEFAULT now()
);


--
-- TOC entry 228 (class 1259 OID 17295)
-- Name: ensemble_memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ensemble_memberships (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    ensemble_id uuid NOT NULL,
    person_id uuid NOT NULL,
    user_role text DEFAULT 'member'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- TOC entry 225 (class 1259 OID 16972)
-- Name: ensembles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ensembles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    plan text DEFAULT 'family'::text NOT NULL,
    status text DEFAULT 'trial'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- TOC entry 230 (class 1259 OID 24906)
-- Name: leads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.leads (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    lead_type text NOT NULL,
    name text NOT NULL,
    email text NOT NULL,
    phone text,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    source text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT leads_lead_type_check CHECK ((lead_type = ANY (ARRAY['family'::text, 'agency'::text])))
);


--
-- TOC entry 224 (class 1259 OID 16853)
-- Name: message_chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.message_chunks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    message_id uuid NOT NULL,
    circle_id uuid NOT NULL,
    chunk_index integer NOT NULL,
    body text NOT NULL,
    context_header text NOT NULL,
    context_summary text NOT NULL,
    embedded_text text NOT NULL,
    embedding public.vector(384),
    sent_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- TOC entry 223 (class 1259 OID 16481)
-- Name: messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.messages (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    circle_id uuid NOT NULL,
    person_id uuid,
    message_type text NOT NULL,
    direction text NOT NULL,
    channel text DEFAULT 'groupme'::text NOT NULL,
    body text NOT NULL,
    raw jsonb,
    sent_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT messages_direction_check CHECK ((direction = ANY (ARRAY['inbound'::text, 'outbound'::text]))),
    CONSTRAINT messages_message_type_check CHECK ((message_type = ANY (ARRAY['inbound'::text, 'check_in'::text, 'digest'::text, 'agent_note'::text, 'prep_packet'::text])))
);


--
-- TOC entry 220 (class 1259 OID 16414)
-- Name: people; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.people (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    external_id text,
    name text NOT NULL,
    email text,
    phone text,
    timezone text DEFAULT 'America/Chicago'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    aliases text[] DEFAULT '{}'::text[],
    notes text,
    ensemble_id uuid,
    date_of_birth date
);


--
-- TOC entry 3556 (class 2606 OID 16516)
-- Name: care_circles care_circles_external_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.care_circles
    ADD CONSTRAINT care_circles_external_id_key UNIQUE (external_id);


--
-- TOC entry 3558 (class 2606 OID 16447)
-- Name: care_circles care_circles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.care_circles
    ADD CONSTRAINT care_circles_pkey PRIMARY KEY (id);


--
-- TOC entry 3561 (class 2606 OID 16470)
-- Name: circle_memberships circle_memberships_circle_id_person_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.circle_memberships
    ADD CONSTRAINT circle_memberships_circle_id_person_id_key UNIQUE (circle_id, person_id);


--
-- TOC entry 3564 (class 2606 OID 16468)
-- Name: circle_memberships circle_memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.circle_memberships
    ADD CONSTRAINT circle_memberships_pkey PRIMARY KEY (id);


--
-- TOC entry 3584 (class 2606 OID 17198)
-- Name: clinical_events clinical_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_events
    ADD CONSTRAINT clinical_events_pkey PRIMARY KEY (id);


--
-- TOC entry 3577 (class 2606 OID 17120)
-- Name: clinical_records clinical_records_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_records
    ADD CONSTRAINT clinical_records_pkey PRIMARY KEY (id);


--
-- TOC entry 3591 (class 2606 OID 24752)
-- Name: clinical_signals clinical_signals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_signals
    ADD CONSTRAINT clinical_signals_pkey PRIMARY KEY (id);


--
-- TOC entry 3587 (class 2606 OID 17311)
-- Name: ensemble_memberships ensemble_memberships_ensemble_id_person_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ensemble_memberships
    ADD CONSTRAINT ensemble_memberships_ensemble_id_person_id_key UNIQUE (ensemble_id, person_id);


--
-- TOC entry 3589 (class 2606 OID 17309)
-- Name: ensemble_memberships ensemble_memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ensemble_memberships
    ADD CONSTRAINT ensemble_memberships_pkey PRIMARY KEY (id);


--
-- TOC entry 3575 (class 2606 OID 16989)
-- Name: ensembles ensembles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ensembles
    ADD CONSTRAINT ensembles_pkey PRIMARY KEY (id);


--
-- TOC entry 3597 (class 2606 OID 24922)
-- Name: leads leads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leads
    ADD CONSTRAINT leads_pkey PRIMARY KEY (id);


--
-- TOC entry 3571 (class 2606 OID 16871)
-- Name: message_chunks message_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_chunks
    ADD CONSTRAINT message_chunks_pkey PRIMARY KEY (id);


--
-- TOC entry 3568 (class 2606 OID 16499)
-- Name: messages messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (id);


--
-- TOC entry 3552 (class 2606 OID 16431)
-- Name: people people_external_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.people
    ADD CONSTRAINT people_external_id_key UNIQUE (external_id);


--
-- TOC entry 3554 (class 2606 OID 16429)
-- Name: people people_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.people
    ADD CONSTRAINT people_pkey PRIMARY KEY (id);


--
-- TOC entry 3573 (class 2606 OID 16873)
-- Name: message_chunks uq_message_chunk; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_chunks
    ADD CONSTRAINT uq_message_chunk UNIQUE (message_id, chunk_index);


--
-- TOC entry 3559 (class 1259 OID 16512)
-- Name: circle_memberships_circle_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX circle_memberships_circle_id_idx ON public.circle_memberships USING btree (circle_id);


--
-- TOC entry 3562 (class 1259 OID 16513)
-- Name: circle_memberships_person_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX circle_memberships_person_id_idx ON public.circle_memberships USING btree (person_id);


--
-- TOC entry 3578 (class 1259 OID 17141)
-- Name: idx_clinical_circle; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clinical_circle ON public.clinical_records USING btree (circle_id);


--
-- TOC entry 3579 (class 1259 OID 17158)
-- Name: idx_clinical_circle_provenance; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clinical_circle_provenance ON public.clinical_records USING btree (circle_id) WHERE (circle_id IS NOT NULL);


--
-- TOC entry 3580 (class 1259 OID 17144)
-- Name: idx_clinical_data; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clinical_data ON public.clinical_records USING gin (data);


--
-- TOC entry 3585 (class 1259 OID 17214)
-- Name: idx_clinical_events_record_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clinical_events_record_id ON public.clinical_events USING btree (record_id);


--
-- TOC entry 3581 (class 1259 OID 17142)
-- Name: idx_clinical_person; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clinical_person ON public.clinical_records USING btree (person_id);


--
-- TOC entry 3582 (class 1259 OID 17157)
-- Name: idx_clinical_person_type_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clinical_person_type_status ON public.clinical_records USING btree (person_id, resource_type, status);


--
-- TOC entry 3592 (class 1259 OID 24781)
-- Name: idx_clinical_signals_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clinical_signals_category ON public.clinical_signals USING btree (signal_category);


--
-- TOC entry 3593 (class 1259 OID 24778)
-- Name: idx_clinical_signals_circle; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clinical_signals_circle ON public.clinical_signals USING btree (circle_id);


--
-- TOC entry 3594 (class 1259 OID 24780)
-- Name: idx_clinical_signals_message; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clinical_signals_message ON public.clinical_signals USING btree (message_id);


--
-- TOC entry 3595 (class 1259 OID 24779)
-- Name: idx_clinical_signals_subject; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clinical_signals_subject ON public.clinical_signals USING btree (subject_id);


--
-- TOC entry 3569 (class 1259 OID 16884)
-- Name: message_chunks_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX message_chunks_embedding_idx ON public.message_chunks USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='10');


--
-- TOC entry 3565 (class 1259 OID 16511)
-- Name: messages_circle_id_message_type_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX messages_circle_id_message_type_idx ON public.messages USING btree (circle_id, message_type);


--
-- TOC entry 3566 (class 1259 OID 16510)
-- Name: messages_circle_id_sent_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX messages_circle_id_sent_at_idx ON public.messages USING btree (circle_id, sent_at DESC);


--
-- TOC entry 3620 (class 2620 OID 17146)
-- Name: clinical_records clinical_records_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER clinical_records_updated_at BEFORE UPDATE ON public.clinical_records FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- TOC entry 3599 (class 2606 OID 16995)
-- Name: care_circles care_circles_ensemble_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.care_circles
    ADD CONSTRAINT care_circles_ensemble_id_fkey FOREIGN KEY (ensemble_id) REFERENCES public.ensembles(id) ON DELETE CASCADE;


--
-- TOC entry 3600 (class 2606 OID 16471)
-- Name: circle_memberships circle_memberships_circle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.circle_memberships
    ADD CONSTRAINT circle_memberships_circle_id_fkey FOREIGN KEY (circle_id) REFERENCES public.care_circles(id) ON DELETE CASCADE;


--
-- TOC entry 3601 (class 2606 OID 16476)
-- Name: circle_memberships circle_memberships_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.circle_memberships
    ADD CONSTRAINT circle_memberships_person_id_fkey FOREIGN KEY (person_id) REFERENCES public.people(id);


--
-- TOC entry 3610 (class 2606 OID 17204)
-- Name: clinical_events clinical_events_confirmed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_events
    ADD CONSTRAINT clinical_events_confirmed_by_fkey FOREIGN KEY (confirmed_by) REFERENCES public.people(id);


--
-- TOC entry 3611 (class 2606 OID 17199)
-- Name: clinical_events clinical_events_record_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_events
    ADD CONSTRAINT clinical_events_record_id_fkey FOREIGN KEY (record_id) REFERENCES public.clinical_records(id);


--
-- TOC entry 3612 (class 2606 OID 17209)
-- Name: clinical_events clinical_events_source_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_events
    ADD CONSTRAINT clinical_events_source_message_id_fkey FOREIGN KEY (source_message_id) REFERENCES public.messages(id);


--
-- TOC entry 3606 (class 2606 OID 17121)
-- Name: clinical_records clinical_records_circle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_records
    ADD CONSTRAINT clinical_records_circle_id_fkey FOREIGN KEY (circle_id) REFERENCES public.care_circles(id) ON DELETE CASCADE;


--
-- TOC entry 3607 (class 2606 OID 17136)
-- Name: clinical_records clinical_records_confirmed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_records
    ADD CONSTRAINT clinical_records_confirmed_by_fkey FOREIGN KEY (confirmed_by) REFERENCES public.people(id) ON DELETE SET NULL;


--
-- TOC entry 3608 (class 2606 OID 17126)
-- Name: clinical_records clinical_records_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_records
    ADD CONSTRAINT clinical_records_person_id_fkey FOREIGN KEY (person_id) REFERENCES public.people(id) ON DELETE CASCADE;


--
-- TOC entry 3609 (class 2606 OID 17131)
-- Name: clinical_records clinical_records_source_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_records
    ADD CONSTRAINT clinical_records_source_message_id_fkey FOREIGN KEY (source_message_id) REFERENCES public.messages(id) ON DELETE SET NULL;


--
-- TOC entry 3615 (class 2606 OID 24763)
-- Name: clinical_signals clinical_signals_circle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_signals
    ADD CONSTRAINT clinical_signals_circle_id_fkey FOREIGN KEY (circle_id) REFERENCES public.care_circles(id);


--
-- TOC entry 3616 (class 2606 OID 24758)
-- Name: clinical_signals clinical_signals_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_signals
    ADD CONSTRAINT clinical_signals_message_id_fkey FOREIGN KEY (message_id) REFERENCES public.messages(id);


--
-- TOC entry 3618 (class 2606 OID 24768)
-- Name: clinical_signals clinical_signals_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_signals
    ADD CONSTRAINT clinical_signals_subject_id_fkey FOREIGN KEY (subject_id) REFERENCES public.people(id);


--
-- TOC entry 3619 (class 2606 OID 24773)
-- Name: clinical_signals clinical_signals_superseded_by_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_signals
    ADD CONSTRAINT clinical_signals_superseded_by_id_fkey FOREIGN KEY (superseded_by_id) REFERENCES public.clinical_signals(id);


--
-- TOC entry 3613 (class 2606 OID 17312)
-- Name: ensemble_memberships ensemble_memberships_ensemble_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ensemble_memberships
    ADD CONSTRAINT ensemble_memberships_ensemble_id_fkey FOREIGN KEY (ensemble_id) REFERENCES public.ensembles(id);


--
-- TOC entry 3614 (class 2606 OID 17317)
-- Name: ensemble_memberships ensemble_memberships_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ensemble_memberships
    ADD CONSTRAINT ensemble_memberships_person_id_fkey FOREIGN KEY (person_id) REFERENCES public.people(id);


--
-- TOC entry 3604 (class 2606 OID 16879)
-- Name: message_chunks message_chunks_circle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_chunks
    ADD CONSTRAINT message_chunks_circle_id_fkey FOREIGN KEY (circle_id) REFERENCES public.care_circles(id);


--
-- TOC entry 3605 (class 2606 OID 16874)
-- Name: message_chunks message_chunks_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_chunks
    ADD CONSTRAINT message_chunks_message_id_fkey FOREIGN KEY (message_id) REFERENCES public.messages(id) ON DELETE CASCADE;


--
-- TOC entry 3602 (class 2606 OID 16500)
-- Name: messages messages_circle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_circle_id_fkey FOREIGN KEY (circle_id) REFERENCES public.care_circles(id) ON DELETE CASCADE;


--
-- TOC entry 3603 (class 2606 OID 16505)
-- Name: messages messages_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_person_id_fkey FOREIGN KEY (person_id) REFERENCES public.people(id);


--
-- TOC entry 3598 (class 2606 OID 16990)
-- Name: people people_ensemble_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.people
    ADD CONSTRAINT people_ensemble_id_fkey FOREIGN KEY (ensemble_id) REFERENCES public.ensembles(id) ON DELETE SET NULL;


-- Completed on 2026-07-07 19:38:48 CDT

--
-- PostgreSQL database dump complete
--

\unrestrict S7875gJsJqccfS1w7XCyj6BHxE8uGpsxz7JaR0PPHPD413dHJSsQ7yKCRhKNDgN

