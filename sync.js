// ======================================================
// APEX OMS - SUPABASE REALTIME SYNC
// ======================================================

(function () {

    "use strict";

    const config = window.APEX_SUPABASE;

    if (!config) {
        console.error("Supabase configuration missing.");
        return;
    }

    const supabase = window.supabase.createClient(
        config.url,
        config.anonKey
    );

    const TABLE = config.table || "oms_state";
    const ROW_ID = config.rowId || "main";

    let isRemoteUpdate = false;
    let isSaving = false;
    let syncTimer = null;
    let lastUpdated = null;

    // --------------------------------------------------
    // Preserve existing OMS save function
    // --------------------------------------------------

    const originalSave = window.save;

    if (typeof originalSave !== "function") {
        console.error("OMS save() function not found.");
        return;
    }

    // --------------------------------------------------
    // Prepare data for cloud
    // --------------------------------------------------

    function getCloudState() {

        const state = JSON.parse(
            JSON.stringify(window.db)
        );

        // Keep local login session private
        delete state.session;

        return state;
    }

    // --------------------------------------------------
    // SAVE TO SUPABASE
    // --------------------------------------------------

    async function saveToSupabase() {

        if (isSaving || isRemoteUpdate) {
            return;
        }

        isSaving = true;

        try {

            const cloudState = getCloudState();

            const { data, error } = await supabase
                .from(TABLE)
                .upsert({
                    id: ROW_ID,
                    state: cloudState,
                    updated_at: new Date().toISOString()
                }, {
                    onConflict: "id"
                })
                .select()
                .single();

            if (error) {
                throw error;
            }

            lastUpdated = data.updated_at;

            console.log("OMS saved to Supabase.");

        } catch (error) {

            console.error(
                "Supabase save error:",
                error
            );

        } finally {

            isSaving = false;

        }

    }

    // --------------------------------------------------
    // Override existing save()
    // --------------------------------------------------

    window.save = function () {

        originalSave.apply(this, arguments);

        if (isRemoteUpdate) {
            return;
        }

        clearTimeout(syncTimer);

        syncTimer = setTimeout(function () {
            saveToSupabase();
        }, 500);

    };

    // --------------------------------------------------
    // LOAD INITIAL DATA
    // --------------------------------------------------

    async function loadFromSupabase() {

        try {

            const { data, error } = await supabase
                .from(TABLE)
                .select("state, updated_at")
                .eq("id", ROW_ID)
                .maybeSingle();

            if (error) {
                throw error;
            }

            if (!data) {

                console.log(
                    "No OMS cloud data found. Uploading local data."
                );

                await saveToSupabase();

                return;
            }

            lastUpdated = data.updated_at;

            applyRemoteState(data.state);

            console.log("OMS cloud data loaded.");

        } catch (error) {

            console.error(
                "Supabase initial load error:",
                error
            );

        }

    }

    // --------------------------------------------------
    // APPLY REMOTE STATE
    // --------------------------------------------------

    function applyRemoteState(remoteState) {

        if (!remoteState) {
            return;
        }

        isRemoteUpdate = true;

        try {

            // Keep current browser login session
            const currentSession = window.db.session;

            window.db = Object.assign(
                {},
                remoteState,
                {
                    session: currentSession
                }
            );

            // Save locally without triggering cloud upload
            originalSave();

            // Refresh current OMS page
            if (typeof window.render === "function") {
                window.render();
            }

        } catch (error) {

            console.error(
                "Remote state application error:",
                error
            );

        } finally {

            isRemoteUpdate = false;

        }

    }

    // --------------------------------------------------
    // REALTIME SUBSCRIPTION
    // --------------------------------------------------

    function startRealtime() {

        supabase
            .channel("apex-oms-realtime")

            .on(
                "postgres_changes",
                {
                    event: "*",
                    schema: "public",
                    table: TABLE,
                    filter: "id=eq." + ROW_ID
                },

                function (payload) {

                    console.log(
                        "Supabase realtime event:",
                        payload.eventType
                    );

                    if (
                        payload.eventType === "INSERT" ||
                        payload.eventType === "UPDATE"
                    ) {

                        const remoteState =
                            payload.new.state;

                        const remoteUpdated =
                            payload.new.updated_at;

                        if (
                            remoteUpdated === lastUpdated
                        ) {
                            return;
                        }

                        lastUpdated = remoteUpdated;

                        applyRemoteState(remoteState);

                    }

                }
            )

            .subscribe(function (status) {

                console.log(
                    "Supabase Realtime:",
                    status
                );

            });

    }

    // --------------------------------------------------
    // INITIALIZE
    // --------------------------------------------------

    async function initializeOMS() {

        console.log(
            "Connecting APEX OMS to Supabase..."
        );

        await loadFromSupabase();

        startRealtime();

    }

    initializeOMS();

})();