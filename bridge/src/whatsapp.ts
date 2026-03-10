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
  media_metadata: MediaMetadata[];
}

export interface InboundMessage {
  id: string;
  sender: string;
  pn: string;
  pushName?: string;
  content: string;
  timestamp: number;
  isGroup: boolean;
  media_metadata: MediaMetadata[];
  mediaBase64?: string;
  mediaType?: string;
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
        const { content, media_metadata } = extracted;

        const isGroup = msg.key.remoteJid?.endsWith('@g.us') || false;

        const outMsg: InboundMessage = {
          id: msg.key.id || '',
          sender: msg.key.remoteJid || '',
          pn: msg.key.remoteJidAlt || '',
          pushName: msg.pushName || undefined,
          content,
          timestamp: msg.messageTimestamp as number,
          isGroup,
          media_metadata,
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

  private extractMessageContent(msg: any): ExtractedMessage | null {
    const message = msg.message;
    if (!message) return null;

    // Text message
    if (message.conversation) {
      return { content: message.conversation, media_metadata: [] };
    }

    // Extended text (reply, link preview)
    if (message.extendedTextMessage?.text) {
      return { content: message.extendedTextMessage.text, media_metadata: [] };
    }

    if (message.imageMessage) {
      const caption = message.imageMessage.caption ? ` ${message.imageMessage.caption}` : '';
      return {
        content: `[Image: ${caption || 'No caption'}]`,
        media_metadata: [this.mediaPlaceholder('image', message.imageMessage)],
      };
    }

    if (message.videoMessage) {
      const caption = message.videoMessage.caption ? ` ${message.videoMessage.caption}` : '';
      return {
        content: `[Video: ${caption || 'No caption'}]`,
        media_metadata: [this.mediaPlaceholder('video', message.videoMessage)],
      };
    }

    if (message.documentMessage) {
      const caption = message.documentMessage.caption ? ` ${message.documentMessage.caption}` : '';
      return {
        content: `[Document: ${caption || 'No caption'}]`,
        media_metadata: [this.mediaPlaceholder('file', message.documentMessage)],
      };
    }

    // Voice/Audio message
    if (message.audioMessage) {
      const mediaType = message.audioMessage.ptt ? 'voice' : 'audio';
      const label = mediaType === 'voice' ? '[Voice Message]' : '[Audio]';
      return {
        content: label,
        media_metadata: [this.mediaPlaceholder(mediaType, message.audioMessage)],
      };
    }

    return null;
  }

  async sendMessage(to: string, text: string): Promise<void> {
    if (!this.sock) {
      throw new Error('Not connected');
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
