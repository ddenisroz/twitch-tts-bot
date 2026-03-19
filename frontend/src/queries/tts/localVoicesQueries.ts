/**
 * Local TTS voice queries proxied through bot_service.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';

import {
    type LocalTtsProvider,
    type LocalVoice,
    type UploadVoiceData,
    localVoicesService
} from '@/services/api/services/localVoicesService';
import { logger } from '@/shared/utils/prodLogger';

import type { AxiosError } from 'axios';

export const localVoicesKeys = {
    all: ['local-voices'] as const,
    list: (provider: LocalTtsProvider) => [...localVoicesKeys.all, provider, 'list'] as const,
};

export const useLocalVoicesQuery = (provider: LocalTtsProvider, enabled: boolean) => {
    return useQuery<LocalVoice[], AxiosError>({
        queryKey: localVoicesKeys.list(provider),
        queryFn: () => localVoicesService.listVoices(provider),
        enabled,
        staleTime: 30 * 1000,
        gcTime: 5 * 60 * 1000,
        retry: 1,
    });
};

export const useUploadVoiceMutation = (provider: LocalTtsProvider) => {
    const queryClient = useQueryClient();

    return useMutation<LocalVoice | null, AxiosError<{ detail?: string }>, UploadVoiceData>({
        mutationFn: (data) => localVoicesService.uploadVoice(provider, data),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: localVoicesKeys.list(provider) });
            toast.success('Voice uploaded');
        },
        onError: (error) => {
            logger.error('Error uploading voice:', error);
            toast.error(error.response?.data?.detail || 'Failed to upload voice');
        },
    });
};

export const useDeleteVoiceMutation = (provider: LocalTtsProvider) => {
    const queryClient = useQueryClient();

    return useMutation<void, AxiosError, number>({
        mutationFn: (voiceId) => localVoicesService.deleteVoice(provider, voiceId),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: localVoicesKeys.list(provider) });
            toast.success('Voice deleted');
        },
        onError: (error) => {
            logger.error('Error deleting voice:', error);
            toast.error('Failed to delete voice');
        },
    });
};

export const useUpdateVoiceSettingsMutation = (provider: LocalTtsProvider) => {
    const queryClient = useQueryClient();

    return useMutation<LocalVoice | null, AxiosError<{ detail?: string }>, { voiceId: number; settings: Record<string, unknown> }>({
        mutationFn: ({ voiceId, settings }) => localVoicesService.updateVoiceSettings(provider, voiceId, settings),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: localVoicesKeys.list(provider) });
            toast.success('Voice settings saved');
        },
        onError: (error) => {
            logger.error('Error updating local voice settings:', error);
            toast.error(error.response?.data?.detail || 'Failed to save voice settings');
        },
    });
};
