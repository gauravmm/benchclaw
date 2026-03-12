/**
 * WhatsApp client wrapper using Baileys.
 * Based on OpenClaw's working implementation.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  downloadMediaMessage,
} from '@whiskeysockets/baileys';

import { Boom } from '@hapi/boom';
import qrcode from 'qrcode-terminal';
import pino from 'pino';

const VERSION = '0.1.0';

export interface MediaMetadata {
  path: string | null;
  media_type: string;
  mime_type: string | null;
  size_bytes: number | null;
  saved_at: string | null;
  source_channel: 'whatsapp';
  original_name?: string | null;
}

interface ExtractedMessage {
  content: string;
  mediaMetadata: MediaMetadata[];
}

export interface InboundMessage {
  id: string;
  chatId: string;
  pn: string;
  pushName?: string;
  senderName?: string;
  nameCache?: Record<string, string>;
  mentions?: string[];
  content: string;
  timestamp: number;
  isGroup: boolean;
  mediaMetadata: MediaMetadata[];
  mediaBase64?: string;
  mediaType?: string;
  replyTo?: string;
  botJids?: string[];
}

export interface WhatsAppClientOptions {
  authDir: string;
  onMessage: (msg: InboundMessage) => void;
  onQR: (qr: string) => void;
  onStatus: (status: string) => void;
}

export class WhatsAppClient {
  private sock: any = null;
  private options: WhatsAppClientOptions;
  private reconnecting = false;
  private contactsByJid = new Map<string, string>();
  private groupParticipantNames = new Map<string, Map<string, string>>();

  constructor(options: WhatsAppClientOptions) {
    this.options = options;
  }

  async connect(): Promise<void> {
    const logger = pino({ level: 'silent' });
    const { state, saveCreds } = await useMultiFileAuthState(this.options.authDir);
    const { version } = await fetchLatestBaileysVersion();

    console.log(`Using Baileys version: ${version.join('.')}`);

    // Create socket following OpenClaw's pattern
    this.sock = makeWASocket({
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, logger),
      },
      version,
      logger,
      printQRInTerminal: false,
      browser: ['nanobot', 'cli', VERSION],
      syncFullHistory: false,
      markOnlineOnConnect: false,
    });

    // Handle WebSocket errors
    if (this.sock.ws && typeof this.sock.ws.on === 'function') {
      this.sock.ws.on('error', (err: Error) => {
        console.error('WebSocket error:', err.message);
      });
    }

    // Handle connection updates
    this.sock.ev.on('connection.update', async (update: any) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        // Display QR code in terminal
        console.log('\n📱 Scan this QR code with WhatsApp (Linked Devices):\n');
        qrcode.generate(qr, { small: true });
        this.options.onQR(qr);
      }

      if (connection === 'close') {
        const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
        const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

        console.log(`Connection closed. Status: ${statusCode}, Will reconnect: ${shouldReconnect}`);
        this.options.onStatus('disconnected');

        if (shouldReconnect && !this.reconnecting) {
          this.reconnecting = true;
          console.log('Reconnecting in 5 seconds...');
          setTimeout(() => {
            this.reconnecting = false;
            this.connect();
          }, 5000);
        }
      } else if (connection === 'open') {
        console.log('✅ Connected to WhatsApp');
        this.options.onStatus('connected');
      }
    });

    // Save credentials on update
    this.sock.ev.on('creds.update', saveCreds);
    this.sock.ev.on('contacts.upsert', (contacts: any[]) => this.ingestContacts(contacts));
    this.sock.ev.on('contacts.update', (contacts: any[]) => this.ingestContacts(contacts));

    // Handle incoming messages
    this.sock.ev.on('messages.upsert', async ({ messages, type }: { messages: any[]; type: string }) => {
      if (type !== 'notify') return;

      for (const msg of messages) {
        // Skip own messages
        if (msg.key.fromMe) continue;

        // Skip status updates
        if (msg.key.remoteJid === 'status@broadcast') continue;

        const extracted = this.extractMessageContent(msg);
        if (!extracted) continue;
        const { content, mediaMetadata } = extracted;
        const context = this.extractContextInfo(msg.message);

        const isGroup = msg.key.remoteJid?.endsWith('@g.us') || false;
        const groupJid = typeof msg.key.remoteJid === 'string' ? msg.key.remoteJid : undefined;
        const senderJid =
          (typeof msg.key.participantAlt === 'string' && msg.key.participantAlt)
          || (typeof msg.key.participant === 'string' && msg.key.participant)
          || (typeof msg.key.remoteJidAlt === 'string' && msg.key.remoteJidAlt)
          || (typeof msg.key.remoteJid === 'string' && msg.key.remoteJid)
          || undefined;
        const senderName = await this.resolveDisplayName(senderJid, groupJid);
        const mentions = this.resolveMentionIds(context.mentionedJids);
        const nameCache = await this.buildNameCache(groupJid);
        const botJids = [
          typeof this.sock?.user?.id === 'string' ? this.sock.user.id : undefined,
          typeof this.sock?.user?.lid === 'string' ? this.sock.user.lid : undefined,
        ].filter((v): v is string => typeof v === 'string' && v.length > 0);

        const outMsg: InboundMessage = {
          id: msg.key.id || '',
          chatId: msg.key.remoteJid || '',
          pn: msg.key.remoteJidAlt || '',
          pushName: msg.pushName || undefined,
          senderName: senderName || undefined,
          nameCache: Object.keys(nameCache).length ? nameCache : undefined,
          mentions: mentions.length ? mentions : undefined,
          content,
          timestamp: msg.messageTimestamp as number,
          isGroup,
          mediaMetadata,
          replyTo: context.replyTo,
          botJids,
        };

        // Download image if present and attach as base64 for Python-side persistence
        if (msg.message?.imageMessage && this.sock) {
          try {
            const buffer = await downloadMediaMessage(
              msg,
              'buffer',
              {},
              { logger, reuploadRequest: this.sock.updateMediaMessage },
            );
            if (buffer) {
              outMsg.mediaBase64 = (buffer as Buffer).toString('base64');
              outMsg.mediaType = msg.message.imageMessage.mimetype || 'image/jpeg';
            }
          } catch (e) {
            console.error('Failed to download WhatsApp image:', e);
          }
        }

        this.options.onMessage(outMsg);
      }
    });
  }

  private mediaPlaceholder(mediaType: string, payload: any): MediaMetadata {
    const maybeLength = payload?.fileLength;
    const asNumber =
      typeof maybeLength === 'number'
        ? maybeLength
        : typeof maybeLength === 'string'
          ? Number.parseInt(maybeLength, 10)
          : null;
    return {
      path: null,
      media_type: mediaType,
      mime_type: typeof payload?.mimetype === 'string' ? payload.mimetype : null,
      size_bytes: Number.isFinite(asNumber) ? asNumber : null,
      saved_at: null,
      source_channel: 'whatsapp',
      original_name: typeof payload?.fileName === 'string' ? payload.fileName : null,
    };
  }

  private canonicalJid(raw: string): string {
    const text = raw.trim().toLowerCase();
    const [localAndDevice, domain] = text.split('@', 2);
    const local = localAndDevice?.split(':', 1)[0] || '';
    return domain ? `${local}@${domain}` : local;
  }

  private rememberContactId(raw: unknown, name: string): void {
    if (typeof raw !== 'string' || !raw) {
      return;
    }
    this.contactsByJid.set(this.canonicalJid(raw), name);
  }

  private ingestContacts(contacts: any[]): void {
    for (const contact of contacts) {
      if (!contact || typeof contact !== 'object') {
        continue;
      }
      const name =
        (typeof contact.name === 'string' && contact.name.trim())
        || (typeof contact.notify === 'string' && contact.notify.trim())
        || (typeof contact.verifiedName === 'string' && contact.verifiedName.trim());
      if (!name) {
        continue;
      }
      this.rememberContactId(contact.id, name);
      this.rememberContactId(contact.lid, name);
      this.rememberContactId(contact.phoneNumber, name);
    }
  }

  private async populateGroupParticipantNames(groupJid: string): Promise<void> {
    if (!this.sock || this.groupParticipantNames.has(groupJid)) {
      return;
    }
    try {
      const metadata = await this.sock.groupMetadata(groupJid);
      const names = new Map<string, string>();
      for (const participant of metadata?.participants || []) {
        const name =
          (typeof participant.name === 'string' && participant.name.trim())
          || (typeof participant.notify === 'string' && participant.notify.trim())
          || (typeof participant.verifiedName === 'string' && participant.verifiedName.trim());
        if (!name) {
          continue;
        }
        if (participant.id) names.set(this.canonicalJid(participant.id), name);
        if (participant.lid) names.set(this.canonicalJid(participant.lid), name);
        if (participant.phoneNumber) names.set(this.canonicalJid(participant.phoneNumber), name);
      }
      this.groupParticipantNames.set(groupJid, names);
    } catch (err) {
      console.error('Failed to fetch WhatsApp group metadata for mention names:', err);
    }
  }

  private resolveMentionIds(
    mentionedJids: string[],
  ): string[] {
    return [...new Set(mentionedJids.map((jid) => this.canonicalJid(jid)).filter(Boolean))];
  }

  private async buildNameCache(groupJid: string | undefined): Promise<Record<string, string>> {
    if (groupJid?.endsWith('@g.us')) {
      await this.populateGroupParticipantNames(groupJid);
    }

    const result: Record<string, string> = {};
    for (const [key, value] of this.contactsByJid.entries()) {
      result[key] = value;
    }
    for (const names of this.groupParticipantNames.values()) {
      for (const [key, value] of names.entries()) {
        result[key] = value;
      }
    }
    const botName = typeof this.sock?.user?.name === 'string' ? this.sock.user.name.trim() : '';
    if (botName) {
      const botJids = [
        typeof this.sock?.user?.id === 'string' ? this.sock.user.id : undefined,
        typeof this.sock?.user?.lid === 'string' ? this.sock.user.lid : undefined,
      ].filter((v): v is string => typeof v === 'string' && v.length > 0);
      for (const jid of botJids) {
        result[this.canonicalJid(jid)] = botName;
      }
    }
    return result;
  }

  private async resolveDisplayName(
    jid: string | undefined,
    groupJid: string | undefined,
  ): Promise<string | undefined> {
    if (!jid) {
      return undefined;
    }
    if (groupJid?.endsWith('@g.us')) {
      await this.populateGroupParticipantNames(groupJid);
    }
    const groupNames = groupJid ? this.groupParticipantNames.get(groupJid) : undefined;
    return groupNames?.get(this.canonicalJid(jid)) || this.contactsByJid.get(this.canonicalJid(jid));
  }

  private unwrapMessageContent(message: any): any {
    let current = message;
    while (current && typeof current === 'object') {
      const next =
        current.ephemeralMessage?.message
        || current.viewOnceMessage?.message
        || current.viewOnceMessageV2?.message
        || current.viewOnceMessageV2Extension?.message
        || current.documentWithCaptionMessage?.message
        || current.editedMessage?.message;
      if (!next || next === current) {
        return current;
      }
      current = next;
    }
    return current;
  }

  private extractContextInfo(message: any): { mentionedJids: string[]; replyTo?: string } {
    const unwrapped = this.unwrapMessageContent(message);
    if (!unwrapped || typeof unwrapped !== 'object') {
      return { mentionedJids: [] };
    }

    const context =
      unwrapped.extendedTextMessage?.contextInfo
      || unwrapped.imageMessage?.contextInfo
      || unwrapped.videoMessage?.contextInfo
      || unwrapped.documentMessage?.contextInfo
      || unwrapped.audioMessage?.contextInfo;

    const mentionedJids = Array.isArray(context?.mentionedJid)
      ? context.mentionedJid.filter((v: unknown): v is string => typeof v === 'string')
      : [];
    const replyTo = typeof context?.participant === 'string' ? context.participant : undefined;
    return { mentionedJids, replyTo };
  }

  private extractMessageContent(msg: any): ExtractedMessage | null {
    const message = this.unwrapMessageContent(msg.message);
    if (!message) return null;

    // Text message
    if (message.conversation) {
      return { content: message.conversation, mediaMetadata: [] };
    }

    // Extended text (reply, link preview)
    if (message.extendedTextMessage?.text) {
      return { content: message.extendedTextMessage.text, mediaMetadata: [] };
    }

    if (message.imageMessage) {
      const caption = message.imageMessage.caption ? ` ${message.imageMessage.caption}` : '';
      return {
        content: `[Image: ${caption || 'No caption'}]`,
        mediaMetadata: [this.mediaPlaceholder('image', message.imageMessage)],
      };
    }

    if (message.videoMessage) {
      const caption = message.videoMessage.caption ? ` ${message.videoMessage.caption}` : '';
      return {
        content: `[Video: ${caption || 'No caption'}]`,
        mediaMetadata: [this.mediaPlaceholder('video', message.videoMessage)],
      };
    }

    if (message.documentMessage) {
      const caption = message.documentMessage.caption ? ` ${message.documentMessage.caption}` : '';
      return {
        content: `[Document: ${caption || 'No caption'}]`,
        mediaMetadata: [this.mediaPlaceholder('file', message.documentMessage)],
      };
    }

    // Voice/Audio message
    if (message.audioMessage) {
      const mediaType = message.audioMessage.ptt ? 'voice' : 'audio';
      const label = mediaType === 'voice' ? '[Voice Message]' : '[Audio]';
      return {
        content: label,
        mediaMetadata: [this.mediaPlaceholder(mediaType, message.audioMessage)],
      };
    }

    return null;
  }

  async sendMessage(
    to: string,
    text: string,
    imageBase64?: string,
    imageMimeType?: string,
  ): Promise<void> {
    if (!this.sock) {
      throw new Error('Not connected');
    }

    if (imageBase64) {
      await this.sock.sendMessage(to, {
        image: Buffer.from(imageBase64, 'base64'),
        mimetype: imageMimeType || 'image/jpeg',
        caption: text || undefined,
      });
      return;
    }

    await this.sock.sendMessage(to, { text });
  }

  async sendTyping(to: string, isTyping: boolean): Promise<void> {
    if (!this.sock) {
      throw new Error('Not connected');
    }

    await this.sock.sendPresenceUpdate(isTyping ? 'composing' : 'paused', to);
  }

  async disconnect(): Promise<void> {
    if (this.sock) {
      this.sock.end(undefined);
      this.sock = null;
    }
  }
}
