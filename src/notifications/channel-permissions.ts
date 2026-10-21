import type { Api } from 'grammy';
import type { ChannelVerification } from '../alerts/channel-service.js';

export function channelVerifier(
  api: Pick<Api, 'getChat' | 'getChatMember'>,
  botId: () => number,
): ChannelVerification {
  return async (channelId, signal) => {
    const apiSignal = signal as unknown as Parameters<Api['getChat']>[1];
    const chat = await api.getChat(channelId, apiSignal);
    if (chat.type !== 'channel' || String(chat.id) !== channelId)
      return { channelId, title: 'مقصد نامعتبر', canPost: false };
    const member = await api.getChatMember(channelId, botId(), apiSignal);
    return {
      channelId,
      title: chat.title,
      canPost:
        member.status === 'administrator' && member.can_post_messages === true,
    };
  };
}
