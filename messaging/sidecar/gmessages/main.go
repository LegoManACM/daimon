// daimon-messages-sidecar: the messaging service's link to Google Messages, paired as a linked device (like
// messages.google.com).
//
// It talks to its parent (daimon_messages/sidecar.py) over stdin/stdout, one JSON object per line, and opens no port of
// its own. The parent sends commands:
//
//	{"cmd":"pair_qr"}                      start QR pairing (a new QR every 30 s, for a few minutes)
//	{"cmd":"pair_google","cookies":{...}}  pair with a Google account's cookies; the phone shows an emoji to tap
//	{"cmd":"cancel_pair"}
//	{"cmd":"unpair"}                       unpair on the phone too and delete the session
//	{"cmd":"backfill","since_ms":N}        send every message since N (Unix ms)
//	{"cmd":"send","req_id":"..","conv":"..","to":"+1..","text":".."}  send a text to a conversation (conv) or a
//	                                       number (to), which opens the conversation if there isn't one
//	{"cmd":"contacts","req_id":".."}       list the phone's contacts
//
// and gets events back:
//
//	{"type":"status","state":"unpaired|pairing|connecting|connected|phone_offline|disconnected|logged_out","detail":""}
//	{"type":"qr","url":"..."}  {"type":"emoji","emoji":"..."}  {"type":"paired"}  {"type":"pair_error","error":"..."}
//	{"type":"conversation",...}  {"type":"message",...}  {"type":"backfill_done","messages":N}
//	{"type":"sent","req_id":"..","ok":true,"conv":"..","error":""}
//	{"type":"contacts","req_id":"..","contacts":[{"id","name","number"}],"error":""}
//
// Logs go to stderr. The session (the pairing keys) is kept in the file given by -session.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/rs/zerolog"
	"go.mau.fi/util/exhttp"

	"go.mau.fi/mautrix-gmessages/pkg/libgm"
	"go.mau.fi/mautrix-gmessages/pkg/libgm/events"
	"go.mau.fi/mautrix-gmessages/pkg/libgm/gmproto"
	"go.mau.fi/mautrix-gmessages/pkg/libgm/util"
)

const (
	qrExpiry      = 30 * time.Second
	qrAttempts    = 6
	convPageSize  = 50
	msgPageSize   = 50
	maxBackfill   = 8 * 24 * time.Hour // the service asks for a week; never more than this whatever it asks
	pairSettleFor = 2 * time.Second    // let the phone save the pairing before reconnecting
	sendTimeout   = 45 * time.Second
	maxTextLen    = 4000
)

type command struct {
	Cmd     string            `json:"cmd"`
	ReqID   string            `json:"req_id,omitempty"`
	Cookies map[string]string `json:"cookies,omitempty"`
	SinceMs int64             `json:"since_ms,omitempty"`
	Conv    string            `json:"conv,omitempty"`
	To      string            `json:"to,omitempty"`
	Text    string            `json:"text,omitempty"`
}

type participant struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	Number string `json:"number"`
	Me     bool   `json:"me,omitempty"`
}

type media struct {
	Name string `json:"name"`
	Mime string `json:"mime"`
	Size int64  `json:"size"`
}

type sidecar struct {
	log         zerolog.Logger
	sessionPath string
	out         *json.Encoder
	outMu       sync.Mutex

	mu      sync.Mutex
	cli     *libgm.Client
	convs   map[string]*gmproto.Conversation
	sims    map[string]*gmproto.SIMCard // by the SIM's own participant ID, from the phone's settings
	pairing context.CancelFunc
	state   string
}

func main() {
	session := flag.String("session", "texts_session.json", "where the pairing keys are kept")
	debug := flag.Bool("debug", false, "verbose logs")
	flag.Parse()
	level := zerolog.InfoLevel
	if *debug {
		level = zerolog.DebugLevel
	}
	s := &sidecar{
		log:         zerolog.New(zerolog.ConsoleWriter{Out: os.Stderr, NoColor: true}).Level(level).With().Timestamp().Logger(),
		sessionPath: *session,
		out:         json.NewEncoder(os.Stdout),
		convs:       map[string]*gmproto.Conversation{},
		sims:        map[string]*gmproto.SIMCard{},
	}
	if auth := s.loadSession(); auth != nil {
		go s.connect(auth)
	} else {
		s.status("unpaired", "")
	}
	in := bufio.NewScanner(os.Stdin)
	in.Buffer(make([]byte, 1<<20), 1<<20)
	for in.Scan() {
		var c command
		if err := json.Unmarshal(in.Bytes(), &c); err != nil {
			s.log.Warn().Err(err).Msg("Bad command")
			continue
		}
		s.handle(c)
	}
	// stdin closed: the service stopped or restarted us.
	s.mu.Lock()
	if s.cli != nil {
		s.cli.Disconnect()
	}
	s.mu.Unlock()
}

// ---- output ----

func (s *sidecar) emit(v map[string]any) {
	s.outMu.Lock()
	defer s.outMu.Unlock()
	if err := s.out.Encode(v); err != nil {
		s.log.Error().Err(err).Msg("Couldn't write to the service")
		os.Exit(1)
	}
}

func (s *sidecar) status(state, detail string) {
	s.mu.Lock()
	s.state = state
	s.mu.Unlock()
	s.emit(map[string]any{"type": "status", "state": state, "detail": detail})
}

// ---- session ----

func (s *sidecar) loadSession() *libgm.AuthData {
	data, err := os.ReadFile(s.sessionPath)
	if err != nil {
		return nil
	}
	auth := libgm.NewAuthData()
	if err := json.Unmarshal(data, auth); err != nil || auth.Browser == nil {
		s.log.Warn().Err(err).Msg("Session file unreadable; pair again")
		return nil
	}
	return auth
}

func (s *sidecar) saveSession(auth *libgm.AuthData) {
	data, err := json.Marshal(auth)
	if err != nil {
		s.log.Error().Err(err).Msg("Couldn't encode the session")
		return
	}
	_ = os.MkdirAll(filepath.Dir(s.sessionPath), 0o700)
	tmp := s.sessionPath + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		s.log.Error().Err(err).Msg("Couldn't save the session")
		return
	}
	if err := os.Rename(tmp, s.sessionPath); err != nil {
		s.log.Error().Err(err).Msg("Couldn't save the session")
	}
}

func (s *sidecar) dropSession(state, why string) {
	s.mu.Lock()
	if s.cli != nil {
		s.cli.SetEventHandler(func(any) {})
		s.cli.Disconnect()
		s.cli = nil
	}
	s.mu.Unlock()
	_ = os.Remove(s.sessionPath)
	s.status(state, why)
}

// ---- commands ----

func (s *sidecar) handle(c command) {
	switch c.Cmd {
	case "pair_qr":
		go s.pairQR()
	case "pair_google":
		go s.pairGoogle(c.Cookies)
	case "cancel_pair":
		s.cancelPairing()
	case "unpair":
		go s.unpair()
	case "backfill":
		go s.backfill(c.SinceMs)
	case "send":
		go s.send(c)
	case "contacts":
		go s.contacts(c.ReqID)
	default:
		s.log.Warn().Str("cmd", c.Cmd).Msg("Unknown command")
	}
}

func (s *sidecar) newClient(auth *libgm.AuthData) *libgm.Client {
	cli := libgm.NewClient(auth, nil, s.log.With().Str("component", "libgm").Logger(), exhttp.ClientSettings{})
	return cli
}

func (s *sidecar) startPairing() (context.Context, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.pairing != nil {
		return nil, false
	}
	ctx, cancel := context.WithTimeout(context.Background(), qrExpiry*qrAttempts+time.Minute)
	s.pairing = cancel
	return ctx, true
}

func (s *sidecar) endPairing() {
	s.mu.Lock()
	if s.pairing != nil {
		s.pairing()
		s.pairing = nil
	}
	s.mu.Unlock()
}

func (s *sidecar) cancelPairing() {
	s.endPairing()
	s.mu.Lock()
	paired := s.cli != nil
	s.mu.Unlock()
	if !paired {
		s.status("unpaired", "")
	}
}

func (s *sidecar) pairQR() {
	ctx, ok := s.startPairing()
	if !ok {
		s.emit(map[string]any{"type": "pair_error", "error": "Pairing is already in progress."})
		return
	}
	defer s.endPairing()
	s.status("pairing", "qr")
	cli := s.newClient(libgm.NewAuthData())
	cli.SetEventHandler(func(evt any) { s.log.Debug().Type("event", evt).Msg("Pre-pairing event") })
	done := make(chan *gmproto.PairedData, 1)
	callback := func(data *gmproto.PairedData) { done <- data }
	cli.PairCallback.Store(&callback)
	fail := func(err error) {
		cli.Disconnect()
		s.emit(map[string]any{"type": "pair_error", "error": err.Error()})
		s.status("unpaired", "")
	}
	if err := cli.FetchConfig(ctx); err != nil {
		fail(fmt.Errorf("couldn't reach Google Messages: %w", err))
		return
	}
	qr, err := cli.StartLogin(ctx)
	if err != nil {
		fail(fmt.Errorf("couldn't start pairing: %w", err))
		return
	}
	s.emit(map[string]any{"type": "qr", "url": qr})
	for attempt := 1; ; attempt++ {
		select {
		case <-done:
			cli.Disconnect()
			s.saveSession(cli.AuthData)
			s.emit(map[string]any{"type": "paired"})
			time.Sleep(pairSettleFor)
			go s.connect(cli.AuthData)
			return
		case <-time.After(qrExpiry):
			if attempt >= qrAttempts {
				fail(errors.New("the QR code wasn't scanned in time"))
				return
			}
			if qr, err = cli.RefreshPhoneRelay(); err != nil {
				fail(fmt.Errorf("couldn't refresh the QR code: %w", err))
				return
			}
			s.emit(map[string]any{"type": "qr", "url": qr})
		case <-ctx.Done():
			fail(errors.New("pairing was cancelled"))
			return
		}
	}
}

func (s *sidecar) pairGoogle(cookies map[string]string) {
	ctx, ok := s.startPairing()
	if !ok {
		s.emit(map[string]any{"type": "pair_error", "error": "Pairing is already in progress."})
		return
	}
	defer s.endPairing()
	s.status("pairing", "google")
	auth := libgm.NewAuthData()
	auth.SetCookies(cookies)
	cli := s.newClient(auth)
	cli.SetEventHandler(func(evt any) { s.log.Debug().Type("event", evt).Msg("Pre-pairing event") })
	fail := func(err error) {
		cli.Disconnect()
		msg := err.Error()
		switch {
		case errors.Is(err, libgm.ErrNoDevicesFound):
			msg = "No phone with Google Messages was found on that Google account."
		case errors.Is(err, libgm.ErrIncorrectEmoji):
			msg = "The wrong emoji was picked on the phone."
		case errors.Is(err, libgm.ErrPairingCancelled):
			msg = "Pairing was cancelled on the phone."
		case errors.Is(err, libgm.ErrPairingTimeout), errors.Is(err, libgm.ErrPairingInitTimeout):
			msg = "The phone didn't respond in time."
		}
		s.emit(map[string]any{"type": "pair_error", "error": msg})
		s.status("unpaired", "")
	}
	if err := cli.FetchConfig(ctx); err != nil {
		fail(fmt.Errorf("couldn't reach Google Messages: %w", err))
		return
	}
	emoji, sess, err := cli.StartGaiaPairing(ctx, ctx)
	if err != nil {
		fail(err)
		return
	}
	s.emit(map[string]any{"type": "emoji", "emoji": emoji})
	if _, err := cli.FinishGaiaPairing(ctx, sess); err != nil {
		fail(err)
		return
	}
	cli.Disconnect()
	s.saveSession(cli.AuthData)
	s.emit(map[string]any{"type": "paired"})
	go s.connect(cli.AuthData)
}

func (s *sidecar) unpair() {
	s.endPairing()
	s.mu.Lock()
	cli := s.cli
	s.mu.Unlock()
	if cli != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		if err := cli.Unpair(ctx); err != nil {
			s.log.Warn().Err(err).Msg("Unpair request failed; forgetting the session anyway")
		}
		cancel()
	}
	s.dropSession("unpaired", "")
}

// ---- connection ----

func (s *sidecar) connect(auth *libgm.AuthData) {
	s.status("connecting", "")
	cli := s.newClient(auth)
	cli.SetEventHandler(s.onEvent)
	s.mu.Lock()
	if s.cli != nil {
		s.cli.SetEventHandler(func(any) {})
		s.cli.Disconnect()
	}
	s.cli = cli
	s.mu.Unlock()
	ctx := context.Background()
	if err := cli.FetchConfig(ctx); err != nil {
		s.log.Warn().Err(err).Msg("Couldn't fetch config")
	}
	if err := cli.Connect(ctx); err != nil {
		if errors.Is(err, events.ErrRequestedEntityNotFound) || errors.Is(err, events.ErrInvalidCredentials) {
			s.dropSession("logged_out", "The phone no longer recognises this pairing. Pair again.")
			return
		}
		s.status("disconnected", err.Error())
		// The service restarts the sidecar after a while; until then there's nothing to do.
		return
	}
	s.status("connected", "")
}

// confirmLoggedOut checks a sign-out notice before forgetting the session. Right after pairing, Google replays a
// backlog to the new connection, and libgm reads a stale sign-out marker in it as a fresh one (it doesn't check
// whether the message is old), which used to drop every new pairing a moment after the emoji. So ask the phone for
// something: only a refusal of our credentials means we're really signed out.
func (s *sidecar) confirmLoggedOut() {
	cli := s.client()
	if cli == nil {
		return
	}
	time.Sleep(3 * time.Second) // let the replayed backlog pass
	if s.client() != cli {
		return // reconnected or unpaired meanwhile
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	_, err := cli.ListConversations(ctx, &gmproto.ListConversationsRequest{
		Count: 1, Folder: gmproto.ListConversationsRequest_INBOX})
	if err != nil && (errors.Is(err, events.ErrInvalidCredentials) || errors.Is(err, events.ErrRequestedEntityNotFound) ||
		strings.Contains(err.Error(), "http 401")) {
		s.dropSession("logged_out", "The Google account signed this device out. Pair again.")
		return
	}
	// Still answering (or just slow, which the ping and listen errors handle): the notice was stale.
	s.log.Warn().AnErr("check", err).Msg("Ignored a sign-out notice; the session still works")
}

func (s *sidecar) client() *libgm.Client {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.cli
}

// onEvent runs on libgm's goroutine: it must not block on requests to the phone.
func (s *sidecar) onEvent(raw any) {
	switch evt := raw.(type) {
	case *events.ListenFatalError:
		if errors.Is(evt.Error, events.ErrInvalidCredentials) || strings.Contains(evt.Error.Error(), "http 401") {
			go s.dropSession("logged_out", "Google Messages signed this device out. Pair again.")
		} else {
			s.status("disconnected", evt.Error.Error())
		}
	case *events.ListenTemporaryError:
		s.status("disconnected", evt.Error.Error())
	case *events.ListenRecovered:
		s.status("connected", "")
	case *events.PhoneNotResponding:
		s.status("phone_offline", "")
	case *events.PhoneRespondingAgain:
		s.status("connected", "")
	case *events.PingFailed:
		if errors.Is(evt.Error, events.ErrRequestedEntityNotFound) {
			go s.dropSession("logged_out", "The phone unpaired this device. Pair again.")
		}
	case *gmproto.RevokePairData:
		go s.dropSession("logged_out", "The phone unpaired this device. Pair again.")
	case *events.GaiaLoggedOut:
		go s.confirmLoggedOut()
	case *events.AuthTokenRefreshed:
		if cli := s.client(); cli != nil {
			s.saveSession(cli.AuthData)
		}
	case *gmproto.UserAlertEvent:
		switch evt.GetAlertType() {
		case gmproto.AlertType_BROWSER_INACTIVE_FROM_TIMEOUT, gmproto.AlertType_BROWSER_INACTIVE_FROM_INACTIVITY,
			gmproto.AlertType_BROWSER_INACTIVE:
			// Google Messages was opened in a browser somewhere else. Take the session back, gently.
			go s.reactivate()
		case gmproto.AlertType_BROWSER_ACTIVE:
			s.status("connected", "")
		}
	case *gmproto.Settings:
		if evt.GetSIMCards() != nil {
			s.mu.Lock()
			for _, sim := range evt.GetSIMCards() {
				s.sims[sim.GetSIMParticipant().GetID()] = sim
			}
			s.mu.Unlock()
		}
	case *gmproto.Conversation:
		s.remember(evt)
		s.emitConversation(evt)
	case *libgm.WrappedMessage:
		s.emitMessage(evt.Message, evt.IsOld, false)
	}
}

func (s *sidecar) reactivate() {
	for _, wait := range []time.Duration{5 * time.Second, 30 * time.Second, 2 * time.Minute} {
		time.Sleep(wait)
		cli := s.client()
		if cli == nil {
			return
		}
		if err := cli.SetActiveSession(context.Background()); err == nil {
			s.status("connected", "")
			return
		}
	}
	s.status("disconnected", "Google Messages is open in another browser.")
}

// ---- conversations and messages ----

func (s *sidecar) remember(conv *gmproto.Conversation) {
	s.mu.Lock()
	s.convs[conv.GetConversationID()] = conv
	s.mu.Unlock()
}

func (s *sidecar) conversation(id string) *gmproto.Conversation {
	s.mu.Lock()
	conv := s.convs[id]
	s.mu.Unlock()
	return conv
}

func participants(conv *gmproto.Conversation) []participant {
	out := []participant{}
	for _, p := range conv.GetParticipants() {
		name := p.GetFullName()
		if name == "" {
			name = p.GetFirstName()
		}
		out = append(out, participant{ID: p.GetID().GetParticipantID(), Name: name, Number: p.GetID().GetNumber(),
			Me: p.GetIsMe()})
	}
	return out
}

func (s *sidecar) emitConversation(conv *gmproto.Conversation) {
	s.emit(map[string]any{
		"type":         "conversation",
		"id":           conv.GetConversationID(),
		"name":         conv.GetName(),
		"group":        conv.GetIsGroupChat(),
		"participants": participants(conv),
		"last_ms":      conv.GetLastMessageTimestamp() / 1000,
		"read_only":    conv.GetReadOnly(),
		"kind":         conv.GetType().String(), // SMS | RCS | ...
	})
}

func outgoing(status gmproto.MessageStatusType) bool {
	return status > 0 && status < 100
}

func (s *sidecar) emitMessage(m *gmproto.Message, old, backfill bool) {
	status := m.GetMessageStatus().GetStatus()
	switch {
	case status == gmproto.MessageStatusType_OUTGOING_DRAFT:
		return
	case status >= 200 && status != gmproto.MessageStatusType_MESSAGE_DELETED:
		return // "X joined", "group created" and the like
	}
	var text []string
	files := []media{}
	for _, info := range m.GetMessageInfo() {
		if c := info.GetMessageContent(); c != nil && c.GetContent() != "" {
			text = append(text, c.GetContent())
		}
		if md := info.GetMediaContent(); md != nil {
			files = append(files, media{Name: md.GetMediaName(), Mime: md.GetMimeType(), Size: md.GetSize()})
		}
	}
	senderID := m.GetParticipantID()
	fromMe := outgoing(status) || m.GetSenderParticipant().GetIsMe()
	name, number := "", ""
	if sp := m.GetSenderParticipant(); sp != nil {
		name, number = sp.GetFullName(), sp.GetID().GetNumber()
	}
	if conv := s.conversation(m.GetConversationID()); conv != nil {
		for _, p := range conv.GetParticipants() {
			if p.GetID().GetParticipantID() == senderID {
				if name == "" {
					name = p.GetFullName()
				}
				if number == "" {
					number = p.GetID().GetNumber()
				}
				fromMe = fromMe || p.GetIsMe()
			}
		}
	} else {
		go s.fetchConversation(m.GetConversationID())
	}
	s.emit(map[string]any{
		"type":     "message",
		"id":       m.GetMessageID(),
		"conv":     m.GetConversationID(),
		"sender":   senderID,
		"name":     name,
		"number":   number,
		"from_me":  fromMe,
		"ts_ms":    m.GetTimestamp() / 1000,
		"text":     strings.Join(text, "\n"),
		"media":    files,
		"status":   status.String(),
		"deleted":  status == gmproto.MessageStatusType_MESSAGE_DELETED,
		"old":      old,
		"backfill": backfill,
	})
}

var fetching sync.Map

func (s *sidecar) fetchConversation(id string) {
	if id == "" {
		return
	}
	if _, busy := fetching.LoadOrStore(id, true); busy {
		return
	}
	defer fetching.Delete(id)
	cli := s.client()
	if cli == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	conv, err := cli.GetConversation(ctx, id)
	if err != nil {
		s.log.Warn().Err(err).Str("conv", id).Msg("Couldn't fetch conversation")
		return
	}
	s.remember(conv)
	s.emitConversation(conv)
}

// backfill sends every message since sinceMs, newest conversations first, never older than maxBackfill.
func (s *sidecar) backfill(sinceMs int64) {
	cli := s.client()
	if cli == nil {
		s.emit(map[string]any{"type": "backfill_done", "messages": 0, "error": "not paired"})
		return
	}
	floor := time.Now().Add(-maxBackfill).UnixMilli()
	if sinceMs < floor {
		sinceMs = floor
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	total := 0
	var cursor *gmproto.Cursor
	for page := 0; page < 10; page++ {
		resp, err := cli.ListConversations(ctx, &gmproto.ListConversationsRequest{
			Count: convPageSize, Folder: gmproto.ListConversationsRequest_INBOX, Cursor: cursor,
		})
		if err != nil {
			s.emit(map[string]any{"type": "backfill_done", "messages": total, "error": err.Error()})
			return
		}
		convs := resp.GetConversations()
		older := false
		for _, conv := range convs {
			if conv.GetLastMessageTimestamp()/1000 < sinceMs {
				older = true
				continue
			}
			s.remember(conv)
			s.emitConversation(conv)
			total += s.backfillConversation(ctx, cli, conv.GetConversationID(), sinceMs)
		}
		cursor = resp.GetCursor()
		if older || len(convs) < convPageSize || cursor == nil {
			break
		}
	}
	s.emit(map[string]any{"type": "backfill_done", "messages": total})
}

func (s *sidecar) backfillConversation(ctx context.Context, cli *libgm.Client, id string, sinceMs int64) int {
	n := 0
	var cursor *gmproto.Cursor
	for page := 0; page < 20; page++ {
		resp, err := cli.FetchMessages(ctx, id, msgPageSize, cursor)
		if err != nil {
			s.log.Warn().Err(err).Str("conv", id).Msg("Couldn't fetch messages")
			return n
		}
		msgs := resp.GetMessages()
		done := len(msgs) < msgPageSize
		for _, m := range msgs { // newest first
			if m.GetTimestamp()/1000 < sinceMs {
				done = true
				continue
			}
			s.emitMessage(m, true, true)
			n++
		}
		cursor = resp.GetCursor()
		if done || cursor == nil {
			return n
		}
	}
	return n
}

// ---- sending ----

func (s *sidecar) send(c command) {
	reply := func(conv string, err error) {
		out := map[string]any{"type": "sent", "req_id": c.ReqID, "ok": err == nil, "conv": conv}
		if err != nil {
			out["error"] = err.Error()
		}
		s.emit(out)
	}
	cli := s.client()
	if cli == nil {
		reply(c.Conv, errors.New("not paired"))
		return
	}
	text := strings.TrimSpace(c.Text)
	switch {
	case text == "":
		reply(c.Conv, errors.New("the text is empty"))
		return
	case len(text) > maxTextLen:
		reply(c.Conv, fmt.Errorf("the text is too long (over %d characters)", maxTextLen))
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), sendTimeout)
	defer cancel()
	conv, err := s.resolveConversation(ctx, cli, c.Conv, c.To)
	if err != nil {
		reply(c.Conv, err)
		return
	}
	id := conv.GetConversationID()
	if conv.GetReadOnly() {
		reply(id, errors.New("this conversation is read-only (it can't be replied to)"))
		return
	}
	outgoingID := conv.GetDefaultOutgoingID()
	sim := conv.GetSimCard()
	if sim == nil {
		s.mu.Lock()
		if sim = s.sims[outgoingID]; sim == nil {
			for _, first := range s.sims {
				sim = first
				break
			}
		}
		s.mu.Unlock()
	}
	tmpID := util.GenerateTmpID()
	resp, err := cli.SendMessage(ctx, &gmproto.SendMessageRequest{
		ConversationID: id,
		MessagePayload: &gmproto.MessagePayload{
			TmpID:          tmpID,
			ConversationID: id,
			ParticipantID:  outgoingID,
			TmpID2:         tmpID,
			MessageInfo: []*gmproto.MessageInfo{{
				Data: &gmproto.MessageInfo_MessageContent{MessageContent: &gmproto.MessageContent{Content: text}},
			}},
		},
		SIMPayload: sim.GetSIMData().GetSIMPayload(),
		TmpID:      tmpID,
	})
	if err == nil && resp.GetStatus() != gmproto.SendMessageResponse_SUCCESS {
		err = fmt.Errorf("the phone didn't accept the message (%s)", resp.GetStatus())
	}
	reply(id, err)
}

// resolveConversation finds a conversation by ID, or opens (or finds) the one with a phone number.
func (s *sidecar) resolveConversation(ctx context.Context, cli *libgm.Client, id, to string) (*gmproto.Conversation, error) {
	if id != "" {
		if conv := s.conversation(id); conv != nil {
			return conv, nil
		}
		conv, err := cli.GetConversation(ctx, id)
		if err != nil {
			return nil, fmt.Errorf("couldn't find that conversation: %w", err)
		}
		s.remember(conv)
		s.emitConversation(conv)
		return conv, nil
	}
	number := strings.TrimSpace(to)
	if number == "" {
		return nil, errors.New("say which conversation or number to send to")
	}
	resp, err := cli.GetOrCreateConversation(ctx, &gmproto.GetOrCreateConversationRequest{
		Numbers: []*gmproto.ContactNumber{{MysteriousInt: 2, Number: number, Number2: number}},
	})
	if err != nil {
		return nil, fmt.Errorf("couldn't open a conversation with %s: %w", number, err)
	}
	conv := resp.GetConversation()
	if conv.GetConversationID() == "" {
		return nil, fmt.Errorf("couldn't open a conversation with %s (%s)", number, resp.GetStatus())
	}
	s.remember(conv)
	s.emitConversation(conv)
	return conv, nil
}

// ---- contacts ----

func (s *sidecar) contacts(reqID string) {
	out := map[string]any{"type": "contacts", "req_id": reqID}
	cli := s.client()
	if cli == nil {
		out["error"] = "not paired"
		s.emit(out)
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	resp, err := cli.ListContacts(ctx)
	if err != nil {
		out["error"] = err.Error()
		s.emit(out)
		return
	}
	list := []map[string]string{}
	for _, c := range resp.GetContacts() {
		list = append(list, map[string]string{"id": c.GetContactID(), "name": c.GetName(),
			"number": c.GetNumber().GetNumber()})
	}
	out["contacts"] = list
	s.emit(out)
}
