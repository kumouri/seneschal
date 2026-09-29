import { describe, it, expect, vi, afterEach } from "vitest";
import {
  recordingMp3Url,
  sendTelegramMessage,
  sendVoicemailAudio,
  telegramConfigured,
  telegramMethodUrl,
} from "../src/notify/telegram";
import { notifyOwner, ownerChannel } from "../src/notify/owner";
import type { Env } from "../src/config";

function smsOnlyEnv(): Env {
  return {
    TWILIO_ACCOUNT_SID: "AC123",
    TWILIO_AUTH_TOKEN: "tok",
    TWILIO_NUMBER_E164: "+15550000000",
    USER_CELL_E164: "+15551112222",
  } as unknown as Env;
}

function telegramEnv(extra: Partial<Env> = {}): Env {
  return { ...smsOnlyEnv(), TELEGRAM_BOT_TOKEN: "123:abc", TELEGRAM_CHAT_ID: "42", ...extra } as Env;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("telegramConfigured / URLs", () => {
  it("needs both the token and the chat id", () => {
    expect(telegramConfigured(smsOnlyEnv())).toBe(false);
    expect(telegramConfigured({ ...smsOnlyEnv(), TELEGRAM_BOT_TOKEN: "123:abc" } as Env)).toBe(false);
    expect(telegramConfigured({ ...smsOnlyEnv(), TELEGRAM_CHAT_ID: "42" } as Env)).toBe(false);
    expect(telegramConfigured({ ...smsOnlyEnv(), TELEGRAM_BOT_TOKEN: " ", TELEGRAM_CHAT_ID: "42" } as Env)).toBe(false);
    expect(telegramConfigured(telegramEnv())).toBe(true);
  });

  it("builds the Bot API method URL", () => {
    expect(telegramMethodUrl("123:abc", "sendMessage")).toBe("https://api.telegram.org/bot123:abc/sendMessage");
    expect(telegramMethodUrl("123:abc", "sendAudio", "https://proxy.test/")).toBe("https://proxy.test/bot123:abc/sendAudio");
  });

  it("asks Twilio for the mp3 rendition of a recording", () => {
    const base = "https://api.twilio.com/2010-04-01/Accounts/AC123/Recordings/RE1";
    expect(recordingMp3Url(base)).toBe(`${base}.mp3`);
    expect(recordingMp3Url(`${base}.wav`)).toBe(`${base}.mp3`);
    expect(recordingMp3Url(`${base}.mp3`)).toBe(`${base}.mp3`);
  });
});

describe("sendTelegramMessage", () => {
  it("POSTs JSON with chat_id + text to sendMessage, in the main chat by default", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status: 200 }));
    await sendTelegramMessage(telegramEnv(), "Message from Alex\nCall back re: dinner");

    const [url, init] = spy.mock.calls[0]!;
    expect(url).toBe("https://api.telegram.org/bot123:abc/sendMessage");
    expect(init!.method).toBe("POST");
    const body = JSON.parse(init!.body as string);
    expect(body.chat_id).toBe("42");
    expect(body.text).toBe("Message from Alex\nCall back re: dinner");
    expect(body.message_thread_id).toBeUndefined();
    expect(body.parse_mode).toBeUndefined(); // plain text — nothing to escape, nothing to lose
  });

  it("files the message under the topic when TELEGRAM_THREAD_ID is set", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status: 200 }));
    await sendTelegramMessage(telegramEnv({ TELEGRAM_THREAD_ID: "77" }), "hi");
    const body = JSON.parse(spy.mock.calls[0]![1]!.body as string);
    expect(body.message_thread_id).toBe("77");
  });

  it("honours a TELEGRAM_API_BASE override", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status: 200 }));
    await sendTelegramMessage(telegramEnv({ TELEGRAM_API_BASE: "https://proxy.test" }), "hi");
    expect(spy.mock.calls[0]![0]).toBe("https://proxy.test/bot123:abc/sendMessage");
  });

  it("throws on a non-2xx Bot API response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("bad", { status: 400 }));
    await expect(sendTelegramMessage(telegramEnv(), "hi")).rejects.toThrow(/Telegram sendMessage failed: 400/);
  });

  it("refuses when not configured", async () => {
    const spy = vi.spyOn(globalThis, "fetch");
    await expect(sendTelegramMessage(smsOnlyEnv(), "hi")).rejects.toThrow(/not configured/);
    expect(spy).not.toHaveBeenCalled();
  });
});

describe("sendVoicemailAudio", () => {
  it("fetches the Twilio mp3 with Basic auth, then uploads it as multipart sendAudio", async () => {
    const spy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(new Uint8Array([1, 2, 3]), { status: 200, headers: { "Content-Type": "audio/mpeg" } }))
      .mockResolvedValueOnce(new Response("{}", { status: 200 }));

    const rec = "https://api.twilio.com/2010-04-01/Accounts/AC123/Recordings/RE1";
    await sendVoicemailAudio(telegramEnv({ TELEGRAM_THREAD_ID: "77" }), rec, "🎙️ Voicemail audio from +15550001111");

    // Leg 1: the auth-protected recording, as mp3.
    const [recUrl, recInit] = spy.mock.calls[0]!;
    expect(recUrl).toBe(`${rec}.mp3`);
    expect((recInit!.headers as Record<string, string>).Authorization).toBe(`Basic ${btoa("AC123:tok")}`);

    // Leg 2: multipart upload to the bot, carrying chat + topic + caption + the file.
    const [tgUrl, tgInit] = spy.mock.calls[1]!;
    expect(tgUrl).toBe("https://api.telegram.org/bot123:abc/sendAudio");
    expect(tgInit!.method).toBe("POST");
    const form = tgInit!.body as FormData;
    expect(form).toBeInstanceOf(FormData);
    expect(form.get("chat_id")).toBe("42");
    expect(form.get("message_thread_id")).toBe("77");
    expect(form.get("caption")).toBe("🎙️ Voicemail audio from +15550001111");
    const audio = form.get("audio") as unknown as File;
    expect(audio).toBeInstanceOf(Blob);
    expect(audio.name).toBe("voicemail.mp3");
    expect(audio.type).toBe("audio/mpeg");
    expect(audio.size).toBe(3);
  });

  it("throws (and never uploads) when the Twilio fetch fails", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("nope", { status: 401 }));
    await expect(sendVoicemailAudio(telegramEnv(), "https://x/RE1", "c")).rejects.toThrow(/recording fetch failed: 401/);
    expect(spy).toHaveBeenCalledTimes(1);
  });
});

describe("notifyOwner — Telegram first, SMS only as the fallback", () => {
  it("picks telegram when configured, sms when not", () => {
    expect(ownerChannel(telegramEnv())).toBe("telegram");
    expect(ownerChannel(smsOnlyEnv())).toBe("sms");
  });

  it("sends over Telegram and never touches Twilio Messages when configured", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status: 200 }));
    const used = await notifyOwner(telegramEnv(), "Blocked spam: +1555");
    expect(used).toBe("telegram");
    expect(spy).toHaveBeenCalledTimes(1);
    expect(String(spy.mock.calls[0]![0])).toContain("api.telegram.org");
  });

  it("falls back to SMS to USER_CELL_E164 when Telegram is not configured", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status: 201 }));
    const used = await notifyOwner(smsOnlyEnv(), "Message from Alex");
    expect(used).toBe("sms");
    expect(spy).toHaveBeenCalledTimes(1);
    const [url, init] = spy.mock.calls[0]!;
    expect(url).toBe("https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json");
    const body = init!.body as URLSearchParams;
    expect(body.get("To")).toBe("+15551112222");
    expect(body.get("Body")).toBe("Message from Alex");
  });

  it("still tries SMS when a configured Telegram send fails", async () => {
    const spy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response("down", { status: 502 }))
      .mockResolvedValueOnce(new Response("{}", { status: 201 }));
    const used = await notifyOwner(telegramEnv(), "hi");
    expect(used).toBe("sms");
    expect(spy).toHaveBeenCalledTimes(2);
    expect(String(spy.mock.calls[1]![0])).toContain("api.twilio.com");
  });
});
