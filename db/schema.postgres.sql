CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);
CREATE TABLE public.game_configs (
    game_id integer NOT NULL,
    config json NOT NULL
);
CREATE TABLE public.game_day_hours (
    id integer NOT NULL,
    game_id integer NOT NULL,
    day integer NOT NULL,
    hour integer NOT NULL,
    visitors integer NOT NULL,
    buyers integer NOT NULL,
    sold_out integer NOT NULL
);
CREATE SEQUENCE public.game_day_hours_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.game_day_hours_id_seq OWNED BY public.game_day_hours.id;
CREATE TABLE public.game_day_types (
    id integer NOT NULL,
    game_id integer NOT NULL,
    day integer NOT NULL,
    person_type character varying(16) NOT NULL,
    visitors integer NOT NULL,
    buyers integer NOT NULL,
    sold_out integer NOT NULL
);
CREATE SEQUENCE public.game_day_types_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.game_day_types_id_seq OWNED BY public.game_day_types.id;
CREATE TABLE public.game_days (
    id integer NOT NULL,
    game_id integer NOT NULL,
    day integer NOT NULL,
    weather character varying(16) NOT NULL,
    temperature numeric(5,1) NOT NULL,
    price numeric(12,2) NOT NULL,
    recipe_ice integer NOT NULL,
    recipe_sugar integer NOT NULL,
    recipe_lemons integer NOT NULL,
    cost_per_cup numeric(12,2),
    bought_ice integer NOT NULL,
    bought_sugar integer NOT NULL,
    bought_lemons integer NOT NULL,
    bought_cups integer NOT NULL,
    inventory_open_ice integer,
    inventory_open_sugar integer,
    inventory_open_lemons integer,
    inventory_open_cups integer,
    spend numeric(12,2) NOT NULL,
    revenue numeric(12,2) NOT NULL,
    profit numeric(12,2) NOT NULL,
    visitors integer NOT NULL,
    buyers integer NOT NULL,
    sold_out integer NOT NULL,
    refused integer NOT NULL,
    popularity numeric(5,4),
    perished_ice integer NOT NULL,
    perished_sugar integer NOT NULL,
    perished_lemons integer NOT NULL,
    perished_cups integer NOT NULL,
    perished_value numeric(12,2) NOT NULL,
    cash_end numeric(12,2) NOT NULL,
    inventory_end_ice integer,
    inventory_end_sugar integer,
    inventory_end_lemons integer,
    inventory_end_cups integer,
    refusal_reasons json NOT NULL
);
CREATE SEQUENCE public.game_days_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.game_days_id_seq OWNED BY public.game_days.id;
CREATE TABLE public.games (
    id integer NOT NULL,
    public_id character varying(32) NOT NULL,
    user_id integer,
    guest_identity character varying(64),
    rng_seed bigint NOT NULL,
    num_days integer NOT NULL,
    days_played integer NOT NULL,
    starting_cash numeric(12,2) NOT NULL,
    final_cash numeric(12,2) NOT NULL,
    total_profit numeric(12,2) NOT NULL,
    total_revenue numeric(12,2) NOT NULL,
    total_spend numeric(12,2) NOT NULL,
    total_visitors integer NOT NULL,
    total_buyers integer NOT NULL,
    total_sold_out integer NOT NULL,
    final_popularity numeric(5,4),
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.games_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.games_id_seq OWNED BY public.games.id;
CREATE TABLE public.live_games (
    state_key character varying(191) NOT NULL,
    value text NOT NULL,
    expires_at timestamp with time zone
);
CREATE TABLE public.user_stats (
    user_id integer NOT NULL,
    games_played integer NOT NULL,
    days_played integer NOT NULL,
    total_cups_sold integer NOT NULL,
    total_visitors integer NOT NULL,
    total_profit numeric(12,2) NOT NULL,
    best_profit numeric(12,2),
    avg_profit numeric(12,2),
    best_game_id integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.users (
    id integer NOT NULL,
    firebase_uid character varying(128) NOT NULL,
    display_name character varying(24),
    email character varying(200),
    photo_url character varying(500),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;
ALTER TABLE ONLY public.game_day_hours ALTER COLUMN id SET DEFAULT nextval('public.game_day_hours_id_seq'::regclass);
ALTER TABLE ONLY public.game_day_types ALTER COLUMN id SET DEFAULT nextval('public.game_day_types_id_seq'::regclass);
ALTER TABLE ONLY public.game_days ALTER COLUMN id SET DEFAULT nextval('public.game_days_id_seq'::regclass);
ALTER TABLE ONLY public.games ALTER COLUMN id SET DEFAULT nextval('public.games_id_seq'::regclass);
ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);
ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);
ALTER TABLE ONLY public.game_configs
    ADD CONSTRAINT pk_game_configs PRIMARY KEY (game_id);
ALTER TABLE ONLY public.game_day_hours
    ADD CONSTRAINT pk_game_day_hours PRIMARY KEY (id);
ALTER TABLE ONLY public.game_day_types
    ADD CONSTRAINT pk_game_day_types PRIMARY KEY (id);
ALTER TABLE ONLY public.game_days
    ADD CONSTRAINT pk_game_days PRIMARY KEY (id);
ALTER TABLE ONLY public.games
    ADD CONSTRAINT pk_games PRIMARY KEY (id);
ALTER TABLE ONLY public.live_games
    ADD CONSTRAINT pk_live_games PRIMARY KEY (state_key);
ALTER TABLE ONLY public.user_stats
    ADD CONSTRAINT pk_user_stats PRIMARY KEY (user_id);
ALTER TABLE ONLY public.users
    ADD CONSTRAINT pk_users PRIMARY KEY (id);
ALTER TABLE ONLY public.game_day_hours
    ADD CONSTRAINT uq_game_day_hours UNIQUE (game_id, day, hour);
ALTER TABLE ONLY public.game_day_types
    ADD CONSTRAINT uq_game_day_types UNIQUE (game_id, day, person_type);
ALTER TABLE ONLY public.game_days
    ADD CONSTRAINT uq_game_days_game_day UNIQUE (game_id, day);
CREATE INDEX ix_games_guest_identity ON public.games USING btree (guest_identity);
CREATE UNIQUE INDEX ix_games_public_id ON public.games USING btree (public_id);
CREATE INDEX ix_games_user_id ON public.games USING btree (user_id);
CREATE INDEX ix_live_games_expires_at ON public.live_games USING btree (expires_at);
CREATE UNIQUE INDEX ix_users_firebase_uid ON public.users USING btree (firebase_uid);
ALTER TABLE ONLY public.game_configs
    ADD CONSTRAINT fk_game_configs_game_id_games FOREIGN KEY (game_id) REFERENCES public.games(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.game_day_hours
    ADD CONSTRAINT fk_game_day_hours_game_id_games FOREIGN KEY (game_id) REFERENCES public.games(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.game_day_types
    ADD CONSTRAINT fk_game_day_types_game_id_games FOREIGN KEY (game_id) REFERENCES public.games(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.game_days
    ADD CONSTRAINT fk_game_days_game_id_games FOREIGN KEY (game_id) REFERENCES public.games(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.games
    ADD CONSTRAINT fk_games_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.user_stats
    ADD CONSTRAINT fk_user_stats_best_game_id_games FOREIGN KEY (best_game_id) REFERENCES public.games(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.user_stats
    ADD CONSTRAINT fk_user_stats_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
