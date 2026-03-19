import { AxiosError } from 'axios';

import apiClient from '@/services/api/client';
import { logger } from '@/shared/utils/prodLogger';

export type LocalTtsProvider = 'f5' | 'qwen';

export interface LocalVoice {
    id: number;
    name: string;
    language?: string | null;
    description?: string | null;
    type?: 'base' | 'custom' | 'global' | 'user';
    voice_type?: 'base' | 'custom' | 'global' | 'user';
    samples_count?: number;
    file_path?: string | null;
    is_active?: boolean;
    created_at?: string;
    reference_text?: string | null;
    cfg_strength?: number | null;
    speed_preset?: 'very_slow' | 'slow' | 'normal' | 'fast' | 'very_fast' | null;
}

interface LocalVoicesResponse {
    success: boolean;
    voices: LocalVoice[];
}

export interface UploadVoiceData {
    name: string;
    file: File;
    sampleText?: string;
}

interface UploadVoiceResponse {
    success: boolean;
    message?: string;
    voice?: LocalVoice | null;
}

interface UpdateVoiceSettingsResponse {
    success: boolean;
    voice?: LocalVoice | null;
}

const normalizeVoiceType = (voice: LocalVoice): 'base' | 'custom' => {
    const candidate = String(voice.type || voice.voice_type || '').trim().toLowerCase();
    return candidate === 'global' || candidate === 'base' ? 'base' : 'custom';
};

const normalizeVoice = (voice: LocalVoice): LocalVoice => {
    const normalizedType = normalizeVoiceType(voice);
    return {
        ...voice,
        type: normalizedType,
        voice_type: normalizedType,
    };
};

export const localVoicesService = {
    async listVoices(provider: LocalTtsProvider): Promise<LocalVoice[]> {
        try {
            const response = await apiClient.get<LocalVoicesResponse>('/api/local-tts/voices', {
                params: { provider },
            });
            return (response.data.voices || []).map(normalizeVoice);
        } catch (error) {
            logger.error('Error loading local voices:', error);
            throw error;
        }
    },

    async uploadVoice(provider: LocalTtsProvider, data: UploadVoiceData): Promise<LocalVoice | null> {
        const formData = new FormData();
        formData.append('provider', provider);
        formData.append('voice_name', data.name);
        formData.append('file', data.file);
        if (data.sampleText) {
            formData.append('sample_text', data.sampleText);
        }

        const response = await apiClient.post<UploadVoiceResponse>('/api/local-tts/voices/upload', formData);
        return response.data.voice ? normalizeVoice(response.data.voice) : null;
    },

    async deleteVoice(provider: LocalTtsProvider, voiceId: number): Promise<void> {
        await apiClient.delete(`/api/local-tts/voices/${voiceId}`, {
            params: { provider },
        });
    },

    async updateVoiceSettings(
        provider: LocalTtsProvider,
        voiceId: number,
        settings: Record<string, unknown>,
    ): Promise<LocalVoice | null> {
        const response = await apiClient.put<UpdateVoiceSettingsResponse>(`/api/local-tts/voices/${voiceId}/settings`, settings, {
            params: { provider },
        });
        return response.data.voice ? normalizeVoice(response.data.voice) : null;
    },
};

export type { AxiosError };
