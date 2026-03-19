import React, { useEffect, useMemo, useState } from 'react';

/* eslint-disable no-alert */
/* eslint-disable @typescript-eslint/no-non-null-assertion */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, CheckCircle2, Edit, Globe, Lock, RefreshCw, Settings, TestTube2, Trash2, Upload, User, XCircle } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

import { API_BASE_URL } from '@/constants';
import { useAuth } from '@/context/AuthContext';
import { useIntegrations } from '@/context/IntegrationsContext';
import { useTts } from '@/context/TtsContext';
import TtsErrorCard from '@/features/tts/components/TtsErrorCard';
import { useWhitelistStatus } from '@/queries/tts/ttsQueries';
import { ttsService } from '@/services/api/services/ttsService';
import {
    deleteUserVoice,
    getGlobalVoices,
    getUserVoices,
    renameUserVoice,
    retranscribeUserVoice,
    testVoice,
    updateUserVoiceSettings,
    uploadUserVoice
} from '@/services/unified-api';
import PageWrapper from '@/shared/components/PageWrapper';
import { Badge } from '@/shared/components/ui/badge';
import { Button } from '@/shared/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/shared/components/ui/card';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/shared/components/ui/dialog';
import { Input } from '@/shared/components/ui/input';
import { Label } from '@/shared/components/ui/label';
import { PageLoader } from '@/shared/components/ui/loader';
import { Slider } from "@/shared/components/ui/slider";
import { Textarea } from '@/shared/components/ui/textarea';
import { useToast } from '@/shared/components/ui/toast';
import { useButtonPosition } from '@/shared/hooks/useButtonPosition';
import { useLoadingState } from '@/shared/hooks/useLoadingState';
import { logger } from '@/shared/utils/prodLogger';


import type { TtsVoice } from '@/types/tts';

interface WhitelistStatus {
    is_whitelisted: boolean;
    can_manage_voices: boolean;
    platform?: 'twitch' | 'vk';
    message?: string;
}

interface VoiceApiResponse {
    data?: TtsVoice[];
}

interface EnabledVoicesResponse {
    enabled_voice_ids?: number[];
}

interface TranscribeResponse {
    data?: { reference_text?: string };
    reference_text?: string;
}

interface MutationError {
    message?: string;
}

interface TestVoiceResponse {
    audio_url?: string;
}

type VoiceProvider = 'f5' | 'qwen';

const PROVIDER_TAB_CLASS =
    'inline-flex items-center -mb-px appearance-none rounded-none border-b-2 border-transparent bg-transparent px-4 pb-3 pt-2 text-sm font-medium transition-colors';
const PROVIDER_TAB_ACTIVE_CLASS = 'border-b-sky-400 text-sky-400';
const PROVIDER_TAB_INACTIVE_CLASS = 'text-muted-foreground hover:text-sky-300';
const SURFACE_CARD_CLASS = 'card-glass border-border/70 bg-card/75 backdrop-blur-sm shadow-none';
const VOICE_CARD_CLASS = 'overflow-hidden rounded-2xl border border-emerald-500/20 bg-emerald-950/10 backdrop-blur-sm shadow-none';
const SECTION_DIVIDER_CLASS = 'border-t border-border/70';

const extractApiErrorMessage = (error: unknown): string | null => {
    if (!error) return null;
    const typedError = error as {
        message?: string;
        response?: {
            status?: number;
            data?: {
                detail?: string;
                message?: string;
                error?: string;
            };
        };
    };

    return (
        typedError.response?.data?.detail ||
        typedError.response?.data?.message ||
        typedError.response?.data?.error ||
        typedError.message ||
        null
    );
};

const VoiceManagementPageContent: React.FC = () => {
    const navigate = useNavigate();
    const { addToast } = useToast();
    const { getButtonPosition: _getButtonPosition } = useButtonPosition();
    const { user, isAuthenticated } = useAuth();
    const { integrations } = useIntegrations();
    const [loading, setLoading] = useState<boolean>(true);

    const isTwitchConnected = integrations?.twitch?.enabled;
    const isVkConnected = integrations?.vk?.enabled;
    const _hasAnyIntegration = isTwitchConnected || isVkConnected;
    const [uploadDialogOpen, setUploadDialogOpen] = useState<boolean>(false);
    const [editDialogOpen, setEditDialogOpen] = useState<boolean>(false);
    const [renameDialogOpen, setRenameDialogOpen] = useState<boolean>(false);
    const [currentVoice, setCurrentVoice] = useState<TtsVoice | null>(null);
    const [newVoiceName, setNewVoiceName] = useState<string>('');
    const [uploadFile, setUploadFile] = useState<File | null>(null);
    const [voiceName, setVoiceName] = useState<string>('');
    const [uploadReferenceText, setUploadReferenceText] = useState<string>('');
    const [testText, setTestText] = useState<string>("Привет, я бы хотел с тобой постримить, если честно, для меня бы это было честью. Постримить с таким великим стримером было бы реально круто.");
    const [isUploading, setIsUploading] = useState<boolean>(false);
    const [isTranscribing, setIsTranscribing] = useState<boolean>(false);
    const [isTestingVoice, setIsTestingVoice] = useState<boolean>(false);
    const [voiceProvider, setVoiceProvider] = useState<VoiceProvider>('f5');
    const [voiceVolumes, _setVoiceVolumes] = useState<Record<string, number>>({});
    const fileInputRef = React.useRef<HTMLInputElement | null>(null);
    const voiceVolumeSaveTimeout = React.useRef<Record<string, NodeJS.Timeout>>({});

    const { initializeTts: _initializeTts, engineStatus, isCheckingHealth, checkTtsHealth: _checkTtsHealth } = useTts();
    const isHealthy = engineStatus.loaded;
    const isChecking = isCheckingHealth;
    const queryClient = useQueryClient();
    let _audioContext: AudioContext | null = null;
    let _audioSource: AudioBufferSourceNode | null = null;

    const showLoader = useLoadingState(isChecking);

    const _loadVoiceVolume = async (_voiceName: string): Promise<number> => {
        return 50.0;
    };

    const saveVoiceVolume = async (_voiceName: string, _volumeLevel: number): Promise<void> => {
        // No-op: volume is managed via UserVoiceSettings in admin panel
    };

    const { data: whitelistStatusData } = useWhitelistStatus({
        enabled: !!user,
        staleTime: 5 * 60 * 1000,
        refetchOnMount: false,
        refetchOnWindowFocus: false,
    });

    const whitelistStatusRaw = (whitelistStatusData as { data?: unknown } | undefined)?.data ?? whitelistStatusData;
    const whitelistStatus = useMemo(() => {
        const whitelistStatusCandidate = whitelistStatusRaw as Partial<WhitelistStatus> | undefined;
        if (
            !whitelistStatusCandidate ||
            (
                typeof whitelistStatusCandidate.is_whitelisted !== 'boolean' &&
                typeof whitelistStatusCandidate.can_manage_voices !== 'boolean'
            )
        ) {
            return undefined;
        }

        return {
            is_whitelisted: Boolean(whitelistStatusCandidate.is_whitelisted),
            can_manage_voices: Boolean(whitelistStatusCandidate.can_manage_voices),
            platform: whitelistStatusCandidate.platform,
            message: whitelistStatusCandidate.message,
        } as WhitelistStatus;
    }, [whitelistStatusRaw]);

    const { data: globalVoicesData = [], isLoading: globalVoicesLoading, isError: globalVoicesError, error: globalVoicesErrorData } = useQuery<TtsVoice[]>({
        queryKey: ['global-voices', voiceProvider],
        queryFn: async () => {
            const response = await getGlobalVoices(voiceProvider);
            const voiceResponse = (response as unknown) as VoiceApiResponse | undefined;
            const payload = voiceResponse?.data || (response as { data?: unknown })?.data || response;
            const data = Array.isArray(payload)
                ? payload
                : ((payload as { voices?: TtsVoice[] })?.voices || (payload as { data?: TtsVoice[] })?.data || []);
            return Array.isArray(data) ? data : [];
        },
        enabled: !!whitelistStatus?.can_manage_voices,
        staleTime: 5 * 60 * 1000,
        refetchOnMount: false,
        refetchOnWindowFocus: false,
    });

    useEffect(() => {
        if (globalVoicesError && globalVoicesErrorData) {
            logger.error('Error loading global voices:', globalVoicesErrorData);
        }
    }, [globalVoicesError, globalVoicesErrorData]);

    const userId = user?.id;
    const { data: userVoicesData = [], isLoading: userVoicesLoading, isError: userVoicesError, error: userVoicesErrorData } = useQuery<TtsVoice[]>({
        queryKey: ['user-voices', userId, voiceProvider],
        queryFn: async () => {
            if (!userId) return [];
            const response = await getUserVoices(userId, voiceProvider);
            const voiceResponse = (response as unknown) as VoiceApiResponse | undefined;
            const payload = voiceResponse?.data || (response as { data?: unknown })?.data || response;
            const data = Array.isArray(payload)
                ? payload
                : ((payload as { voices?: TtsVoice[] })?.voices || (payload as { data?: TtsVoice[] })?.data || []);
            return Array.isArray(data) ? data : [];
        },
        enabled: !!userId,
        staleTime: 5 * 60 * 1000,
        refetchOnMount: false,
        refetchOnWindowFocus: false,
    });

    useEffect(() => {
        if (userVoicesError && userVoicesErrorData) {
            logger.error('Error loading user voices:', userVoicesErrorData);
        }
    }, [userVoicesError, userVoicesErrorData]);

    const uploadVoiceMutation = useMutation({
        mutationFn: async ({ userId, formData }: { userId: number; formData: FormData }) => {
            setIsUploading(true);
            return await uploadUserVoice(userId, formData, voiceProvider);
        },
        onSuccess: () => {
            addToast({ type: 'success', title: 'Успех', message: 'Голос успешно загружен!' });
            setUploadDialogOpen(false);
            setUploadFile(null);
            setVoiceName('');
            setUploadReferenceText('');
            if (fileInputRef.current) {
                fileInputRef.current.value = '';
            }
            queryClient.invalidateQueries({ queryKey: ['user-voices', userId, voiceProvider] });
        },
        onError: (error: unknown) => {
            const mutationError = error as MutationError;
            addToast({ type: 'error', title: 'Ошибка', message: mutationError.message || 'Не удалось загрузить голос.' });
        },
        onSettled: () => {
            setIsUploading(false);
        }
    });

    const deleteVoiceMutation = useMutation({
        mutationFn: async ({ voiceId, userId, voiceName: _voiceName }: { voiceId: number; userId: number; voiceName: string }) => {
            return await deleteUserVoice(String(voiceId), userId, voiceProvider);
        },
        onSuccess: (_data: unknown, variables: { voiceId: number; userId: number; voiceName: string }) => {
            addToast({ type: 'success', title: 'Успех', message: `Голос "${variables.voiceName}" удалён.` });
            queryClient.invalidateQueries({ queryKey: ['user-voices', userId, voiceProvider] });
        },
        onError: (error: unknown) => {
            const mutationError = error as MutationError;
            addToast({ type: 'error', title: 'Ошибка', message: mutationError.message || 'Не удалось удалить голос.' });
        }
    });

    const renameVoiceMutation = useMutation({
        mutationFn: async ({ voiceId, userId, newName }: { voiceId: number; userId: number; newName: string }) => {
            return await renameUserVoice(voiceId, userId, newName, voiceProvider);
        },
        onSuccess: () => {
            addToast({ type: 'success', title: 'Успех', message: 'Голос успешно переименован!' });
            setRenameDialogOpen(false);
            setEditDialogOpen(false);
            queryClient.invalidateQueries({ queryKey: ['user-voices', userId, voiceProvider] });
        },
        onError: (error: unknown) => {
            const mutationError = error as MutationError;
            addToast({ type: 'error', title: 'Ошибка', message: mutationError.message || 'Не удалось переименовать голос.' });
        }
    });

    const updateVoiceSettingsMutation = useMutation({
        mutationFn: async ({ voiceId, userId, settings }: { voiceId: number; userId: number; settings: Record<string, unknown> }) => {
            return await updateUserVoiceSettings(voiceId, userId, settings, voiceProvider);
        },
        onSuccess: () => {
            setEditDialogOpen(false);
            queryClient.invalidateQueries({ queryKey: ['user-voices', userId, voiceProvider] });
            queryClient.invalidateQueries({ queryKey: ['global-voices', voiceProvider] });
        },
        onError: (error: unknown) => {
            const mutationError = error as MutationError;
            addToast({ type: 'error', title: 'Ошибка', message: mutationError.message || 'Не удалось обновить настройки.' });
            queryClient.invalidateQueries({ queryKey: ['user-voices', userId, voiceProvider] });
            queryClient.invalidateQueries({ queryKey: ['global-voices', voiceProvider] });
        }
    });

    const transcribeVoiceMutation = useMutation({
        mutationFn: async ({ voiceId, userId }: { voiceId: number; userId: number }) => {
            setIsTranscribing(true);
            return await retranscribeUserVoice(voiceId, userId, undefined, voiceProvider);
        },
        onSuccess: (response: unknown) => {
            const transcribeResponse = response as TranscribeResponse;
            const newReferenceText = transcribeResponse?.data?.reference_text || transcribeResponse?.reference_text;
            if (newReferenceText && currentVoice) {
                setCurrentVoice({ ...currentVoice, reference_text: newReferenceText });
                addToast({ type: 'success', title: 'Успех', message: 'Референсный текст обновлён!' });
            }
            queryClient.invalidateQueries({ queryKey: ['user-voices', userId, voiceProvider] });
        },
        onError: (error: unknown) => {
            const mutationError = error as MutationError;
            addToast({ type: 'error', title: 'Ошибка', message: mutationError.message || 'Не удалось перетранскрибировать голос.' });
        },
        onSettled: () => {
            setIsTranscribing(false);
        }
    });

    const {
        data: enabledVoicesData,
        isLoading: _enabledVoicesLoading,
        isError: enabledVoicesError,
        error: enabledVoicesErrorData,
    } = useQuery<number[]>({
        queryKey: ['enabled-voices', userId, voiceProvider],
        queryFn: async () => {
            if (!userId) return [];
            const response = await ttsService.getEnabledVoices(userId, voiceProvider);
            const enabledResponse = response.data as EnabledVoicesResponse;
            return enabledResponse.enabled_voice_ids || [];
        },
        enabled: !!userId && !!whitelistStatus?.can_manage_voices,
        staleTime: 5 * 60 * 1000,
        refetchOnMount: false,
        refetchOnWindowFocus: false,
    });

    const updateEnabledVoicesMutation = useMutation({
        mutationFn: async ({ userId, voiceIds }: { userId: number; voiceIds: number[] }) => {
            return await ttsService.saveEnabledVoices(userId, voiceIds, voiceProvider);
        },
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['enabled-voices', userId, voiceProvider] });
        },
        onError: (error: unknown) => {
            logger.error('Error updating enabled voices:', error);
            addToast({ type: 'error', title: 'Ошибка', message: 'Не удалось обновить включенные голоса' });
        }
    });

    const globalVoices = globalVoicesData ?? [];
    const userVoices = userVoicesData ?? [];
    const enabledVoiceIds = enabledVoicesData ?? [];
    const voicesServiceErrorMessage =
        extractApiErrorMessage(globalVoicesErrorData) ||
        extractApiErrorMessage(userVoicesErrorData) ||
        extractApiErrorMessage(enabledVoicesErrorData) ||
        'Сервис голосов временно недоступен';
    const hasVoicesServiceError = globalVoicesError || userVoicesError || enabledVoicesError;
    const shownVoiceServiceErrorRef = React.useRef<string | null>(null);

    useEffect(() => {
        if (enabledVoicesError && enabledVoicesErrorData) {
            logger.error('Error loading enabled voices:', enabledVoicesErrorData);
        }
    }, [enabledVoicesError, enabledVoicesErrorData]);

    useEffect(() => {
        if (!hasVoicesServiceError) {
            shownVoiceServiceErrorRef.current = null;
            return;
        }

        if (shownVoiceServiceErrorRef.current === voicesServiceErrorMessage) {
            return;
        }

        shownVoiceServiceErrorRef.current = voicesServiceErrorMessage;
        addToast({
            type: 'error',
            title: 'Сервис голосов недоступен',
            message: voicesServiceErrorMessage,
        });
    }, [addToast, hasVoicesServiceError, voicesServiceErrorMessage]);

    useEffect(() => {
        if (whitelistStatus) {
            logger.info('Voice management whitelist status:', {
                isWhitelisted: whitelistStatus.is_whitelisted,
                canManageVoices: whitelistStatus.can_manage_voices,
                platform: whitelistStatus.platform,
                message: whitelistStatus.message,
                user: user?.id,
                isGuest: false
            });
        }
    }, [whitelistStatus, user]);

    useEffect(() => {
        if (!whitelistStatus?.can_manage_voices) {
            setLoading(false);
            return;
        }
        setLoading(globalVoicesLoading || userVoicesLoading);
    }, [globalVoicesLoading, userVoicesLoading, whitelistStatus]);

    const handleFileUpload = (event: React.ChangeEvent<HTMLInputElement>): void => {
        event.stopPropagation();

        const file = event.target.files?.[0];

        if (!file) {
            return;
        }

        const supportedFormats = ['.wav', '.mp3', '.flac', '.ogg', '.m4a', '.aac', '.wma', '.aiff', '.au'];
        const fileExtension = file.name.toLowerCase().substring(file.name.lastIndexOf('.'));

        if (!supportedFormats.includes(fileExtension)) {
            addToast({
                type: 'error',
                title: 'Ошибка',
                message: `Неподдерживаемый формат файла. Поддерживаемые форматы: ${supportedFormats.join(', ')}`
            });
            if (event.target) {
                event.target.value = '';
            }
            return;
        }

        setUploadFile(file);
        const nameWithoutExt = file.name.replace(/\.[^/.]+$/, "");
        setVoiceName(nameWithoutExt);
    };

    const handleUpload = async (_event: React.MouseEvent<HTMLButtonElement>): Promise<void> => {
        if (!uploadFile || !voiceName.trim()) {
            addToast({ type: 'error', title: 'Ошибка', message: 'Выберите файл и введите имя голоса.' });
            return;
        }

        const uploadUserId = user?.id;
        if (!uploadUserId) {
            addToast({ type: 'error', title: 'Ошибка', message: 'Не удалось определить пользователя.' });
            return;
        }

        const formData = new FormData();
        formData.append('file', uploadFile);
        formData.append('voice_name', voiceName.trim());
        formData.append('user_id', uploadUserId.toString());
        if (uploadReferenceText.trim()) {
            formData.append('reference_text', uploadReferenceText.trim());
            formData.append('sample_text', uploadReferenceText.trim());
        }

        uploadVoiceMutation.mutate({ userId: uploadUserId, formData });
    };

    const handleDelete = async (voiceId: number, voiceType?: string): Promise<void> => {
        if (!whitelistStatus?.can_manage_voices) {
            addToast({
                type: 'error',
                title: 'Ошибка',
                message: 'У вас нет доступа к удалению голосов. Обратитесь к администратору.'
            });
            return;
        }

        if (voiceType === 'global') {
            addToast({ type: 'error', title: 'Ошибка', message: 'Вы не можете удалять глобальные голоса.' });
            return;
        }

        const voiceToDelete = userVoices.find(v => v.id === voiceId);
        if (!voiceToDelete || !user || !window.confirm(`Вы уверены, что хотите удалить свой голос "${voiceToDelete.name}"?`)) {
            return;
        }

        deleteVoiceMutation.mutate({
            voiceId,
            userId: user.id,
            voiceName: voiceToDelete.name
        });
    };

    const handleEdit = (voice: TtsVoice): void => {
        setCurrentVoice({ ...voice });
        setEditDialogOpen(true);
    };

    const handleTranscribe = async (): Promise<void> => {
        if (!currentVoice || !user) return;

        if (currentVoice.voice_type === 'global') {
            addToast({ type: 'error', title: 'Ошибка', message: 'Вы не можете изменять глобальные голоса.' });
            return;
        }

        transcribeVoiceMutation.mutate({
            voiceId: currentVoice.id,
            userId: user.id
        });
    };

    const handleReferenceTextChange = (value: string): void => {
        setCurrentVoice(prev => prev ? { ...prev, reference_text: value } : null);
    };

    const handleRenameVoice = (): void => {
        if (!currentVoice) return;

        if (currentVoice.voice_type === 'global') {
            addToast({ type: 'error', title: 'Ошибка', message: 'Вы не можете переименовывать глобальные голоса.' });
            return;
        }

        setNewVoiceName(currentVoice.name);
        setRenameDialogOpen(true);
    };

    const handleConfirmRename = async (): Promise<void> => {
        if (!currentVoice || !user || !newVoiceName.trim()) return;

        if (newVoiceName.trim() === currentVoice.name) {
            setRenameDialogOpen(false);
            return;
        }

        renameVoiceMutation.mutate({
            voiceId: currentVoice.id,
            userId: user.id,
            newName: newVoiceName.trim()
        });
    };

    const handleSaveSettings = async (): Promise<void> => {
        if (!currentVoice || !user) return;

        const settings = {
            cfg_strength: currentVoice.cfg_strength,
            speed_preset: currentVoice.speed_preset,
            reference_text: currentVoice.reference_text
        };

        if (currentVoice.voice_type === 'global') {
            queryClient.setQueryData(['global-voices', voiceProvider], (prev: TtsVoice[] = []) => prev.map(voice =>
                voice.id === currentVoice.id
                    ? { ...voice, ...settings }
                    : voice
            ));
        } else {
            queryClient.setQueryData(['user-voices', userId, voiceProvider], (prev: TtsVoice[] = []) => prev.map(voice =>
                voice.id === currentVoice.id
                    ? { ...voice, ...settings }
                    : voice
            ));
        }

        updateVoiceSettingsMutation.mutate(
            { voiceId: currentVoice.id, userId: user.id, settings },
            {
                onSuccess: () => {
                    addToast({
                        type: 'success',
                        title: 'Успех',
                        message: currentVoice.voice_type === 'global'
                            ? 'Настройки применены к вашему профилю'
                            : 'Настройки голоса сохранены!'
                    });
                },
                onError: () => {
                    // Rollback при ошибке
                }
            }
        );
    };

    const _playAudio = (buffer: ArrayBuffer): void => {
        if (_audioSource) {
            _audioSource.stop();
        }
        const AudioContextClass = window.AudioContext || (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
        if (!AudioContextClass) {
            addToast({ type: 'error', title: 'Ошибка', message: 'AudioContext не поддерживается в вашем браузере.' });
            return;
        }
        _audioContext = new AudioContextClass();
        _audioSource = _audioContext.createBufferSource();
        _audioContext.decodeAudioData(buffer, (decodedBuffer) => {
            if (_audioSource) {
                _audioSource.buffer = decodedBuffer;
                _audioSource.connect(_audioContext!.destination);
                _audioSource.start(0);
            }
        }, (error) => {
            logger.error('Error decoding audio data', error);
            addToast({ type: 'error', title: 'Ошибка', message: 'Не удалось воспроизвести аудио.' });
        });
    };

    const handleTestVoice = async (): Promise<void> => {
        if (!currentVoice || !user) return;

        setIsTestingVoice(true);
        try {
            logger.log('Testing voice with parameters:', {
                name: currentVoice.name,
                cfg_strength: currentVoice.cfg_strength,
                speed_preset: currentVoice.speed_preset,
                volume: voiceVolumes[currentVoice.name] || 50
            });

            const response = await testVoice(currentVoice.id, testText, voiceProvider);

            const testResponse = response.data as TestVoiceResponse;
            const audioUrl = testResponse.audio_url;
            if (audioUrl) {
                try {
                    let fullAudioUrl = audioUrl;
                    if (!audioUrl.startsWith('http')) {
                        // Use API_BASE_URL because the test request is sent to the Main API (Bot Service)
                        // and it returns a path relative to itself (or served via its static files)
                        fullAudioUrl = `${API_BASE_URL}${audioUrl}`;
                    }

                    logger.log('Playing test audio:', fullAudioUrl);
                    const audio = new Audio(fullAudioUrl);

                    const volumeLevel = voiceVolumes[currentVoice.name] || 50;
                    audio.volume = volumeLevel / 100;

                    audio.oncanplaythrough = () => {
                        logger.log('Test audio ready to play with volume:', audio.volume);
                        audio.play().catch((e: unknown) => {
                            logger.error("Test audio play failed:", e);
                            addToast({ type: 'error', title: 'Ошибка', message: 'Не удалось воспроизвести аудио.' });
                        });
                    };

                    audio.onended = () => {
                        logger.log('Test audio playback ended');
                    };

                    audio.onerror = (e: unknown) => {
                        logger.error("Error loading test audio:", fullAudioUrl, e);
                        addToast({ type: 'error', title: 'Ошибка', message: 'Не удалось загрузить аудио файл.' });
                    };

                    audio.load();
                } catch (error: unknown) {
                    logger.error("Error creating audio:", error);
                    addToast({ type: 'error', title: 'Ошибка', message: 'Не удалось создать аудио объект.' });
                }
            } else {
                addToast({ type: 'error', title: 'Ошибка', message: 'Не удалось получить аудио для воспроизведения.' });
            }
        } catch (error: unknown) {
            const mutationError = error as MutationError;
            addToast({ type: 'error', title: 'Ошибка', message: mutationError.message || 'Не удалось протестировать голос.' });
        } finally {
            setIsTestingVoice(false);
        }
    };

    const _handleUpdateSettings = async (): Promise<void> => {
        if (!currentVoice || !user) return;
        try {
            await updateUserVoiceSettings(currentVoice.id, user.id, {
                cfg_strength: currentVoice.cfg_strength
            }, voiceProvider);
            addToast({ type: 'success', title: 'Успех', message: `Настройки голоса "${currentVoice.name}" обновлены.` });
            setEditDialogOpen(false);
            queryClient.invalidateQueries({ queryKey: ['user-voices', userId, voiceProvider] });
            queryClient.invalidateQueries({ queryKey: ['global-voices', voiceProvider] });
        } catch (error: unknown) {
            const mutationError = error as MutationError;
            addToast({ type: 'error', title: 'Ошибка', message: mutationError.message || 'Не удалось обновить настройки.' });
        }
    };

    const _handleSliderChange = (value: number[], field: string): void => {
        if (currentVoice) {
            setCurrentVoice(prev => prev ? ({ ...prev, [field]: value[0] }) : null);
        }
    };

    const handleToggleVoiceEnabled = async (voiceId: number): Promise<void> => {
        if (!userId) return;

        const isCurrentlyEnabled = enabledVoiceIds.includes(voiceId);
        const newEnabledIds = isCurrentlyEnabled
            ? enabledVoiceIds.filter(id => id !== voiceId)
            : [...enabledVoiceIds, voiceId];

        if (newEnabledIds.length === 0) {
            addToast({ type: 'error', title: 'Ошибка', message: 'Необходимо оставить хотя бы один голос включенным' });
            return;
        }

        updateEnabledVoicesMutation.mutate({ userId, voiceIds: newEnabledIds });
    };

    const renderVoiceCard = (voice: TtsVoice, scope: 'user' | 'global') => {
        const isEnabled = enabledVoiceIds.includes(voice.id);
        const isGlobalVoice = scope === 'global';

        return (
            <Card
                key={`${scope}-${voice.id}`}
                className={`${VOICE_CARD_CLASS} flex min-h-[148px] flex-col ${isEnabled ? 'border-emerald-400/45 bg-emerald-500/[0.08]' : ''}`}
            >
                <CardHeader className="space-y-2 p-3.5 pb-2">
                    <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                            <CardTitle className="truncate text-sm font-semibold text-foreground">
                                {voice.name}
                            </CardTitle>
                        </div>

                        {!isGlobalVoice && (
                            <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                onClick={() => handleDelete(voice.id, voice.voice_type)}
                                className="h-8 w-8 shrink-0 p-0 text-red-300 hover:bg-red-500/10 hover:text-red-200"
                                title="Удалить голос"
                            >
                                <Trash2 className="h-4 w-4" />
                            </Button>
                        )}
                    </div>
                    <div className="flex">
                        <Badge
                            variant="outline"
                            className={`justify-center px-3 py-1 ${isEnabled
                                ? 'border-emerald-400/45 bg-emerald-500/10 text-emerald-300'
                                : 'border-emerald-500/15 bg-background/40 text-muted-foreground'}`}
                        >
                            {isEnabled ? 'Активен' : 'Выкл'}
                        </Badge>
                    </div>
                </CardHeader>
                <CardContent className="mt-auto px-3.5 pb-3.5 pt-0">
                    <div className="grid grid-cols-2 gap-1.5">
                        <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            onClick={() => handleToggleVoiceEnabled(voice.id)}
                            title={isEnabled ? 'Убрать из пула' : 'Добавить в пул'}
                            aria-label={isEnabled ? 'Убрать из пула' : 'Добавить в пул'}
                            className={isEnabled
                                ? 'h-8 justify-center border-blue-500/35 bg-blue-500/12 text-blue-200 hover:border-blue-400/45 hover:bg-blue-500/16 hover:text-blue-100'
                                : 'h-8 justify-center border-blue-500/35 bg-blue-500/12 text-blue-200 hover:border-blue-400/45 hover:bg-blue-500/16 hover:text-blue-100'}
                        >
                            {isEnabled ? (
                                <>
                                    <XCircle className="h-3.5 w-3.5" />
                                    Из пула
                                </>
                            ) : (
                                <>
                                    <CheckCircle2 className="h-3.5 w-3.5" />
                                    В пул
                                </>
                            )}
                        </Button>
                        <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            onClick={() => handleEdit(voice)}
                            title="Настроить"
                            aria-label="Настроить"
                            className="h-8 justify-center border-blue-500/35 bg-blue-500/12 text-blue-200 hover:border-blue-400/45 hover:bg-blue-500/16 hover:text-blue-100"
                        >
                            <Settings className="h-3.5 w-3.5" />
                            Настроить
                        </Button>
                    </div>
                </CardContent>
            </Card>
        );
    };

    if (!isAuthenticated) {
        return (
            <PageWrapper title="Управление голосами">
                <Card className="card-glass">
                    <CardContent className="pt-16 pb-16 flex flex-col items-center justify-center text-center space-y-6">
                        <div className="w-20 h-20 rounded-full bg-gray-800 flex items-center justify-center">
                            <AlertCircle className="w-10 h-10 text-gray-500" />
                        </div>
                        <div className="space-y-2 max-w-md">
                            <h3 className="text-xl font-semibold text-gray-200">
                                Требуется авторизация
                            </h3>
                            <p className="text-gray-400 text-sm">
                                Для использования управления голосами необходимо войти в систему и подключить хотя бы одну платформу (Twitch или VK Live)
                            </p>
                        </div>
                        <Button
                            onClick={() => navigate('/login')}
                            className="gap-2"
                        >
                            <Settings className="w-4 h-4" />
                            Войти в систему
                        </Button>
                    </CardContent>
                </Card>
            </PageWrapper>
        );
    }

    if (showLoader) {
        return (
            <PageWrapper title="Управление голосами">
                <div className="flex justify-center items-center min-h-[min(400px,60vh)]">
                    <PageLoader />
                </div>
            </PageWrapper>
        );
    }

    if (!isTwitchConnected && !isVkConnected) {
        return (
            <PageWrapper title="Управление голосами">
                <Card className="card-glass">
                    <CardContent className="pt-16 pb-16 flex flex-col items-center justify-center text-center space-y-6">
                        <div className="w-20 h-20 rounded-full bg-gray-800 flex items-center justify-center">
                            <AlertCircle className="w-10 h-10 text-gray-500" />
                        </div>
                        <div className="space-y-2 max-w-md">
                            <h3 className="text-xl font-semibold text-gray-200">
                                Нет подключенных интеграций
                            </h3>
                            <p className="text-gray-400 text-sm">
                                Для использования управления голосами необходимо подключить хотя бы одну платформу (Twitch или VK Live)
                            </p>
                        </div>
                        <Button
                            onClick={() => navigate('/dashboard/settings')}
                            className="gap-2"
                        >
                            <Settings className="w-4 h-4" />
                            Перейти в настройки
                        </Button>
                    </CardContent>
                </Card>
            </PageWrapper>
        );
    }

    if (!isHealthy && !isChecking) {
        return (
            <PageWrapper title="Управление голосами">
                <TtsErrorCard
                    title="TTS сервер недоступен"
                    description="В данный момент сервис TTS недоступен. Управление голосами временно отключено."
                    suggestion="Попробуйте обновить страницу через несколько минут."
                />
            </PageWrapper>
        );
    }

    return (
        <PageWrapper
            title="Управление голосами"
            description={
                whitelistStatus && !whitelistStatus.can_manage_voices
                    ? whitelistStatus.message
                    : ""
            }
        >
            <div className="mb-4 border-b border-border">
                <div className="flex items-center gap-6">
                    <button
                        type="button"
                        onClick={() => setVoiceProvider('f5')}
                        className={`${PROVIDER_TAB_CLASS} ${voiceProvider === 'f5'
                            ? PROVIDER_TAB_ACTIVE_CLASS
                            : PROVIDER_TAB_INACTIVE_CLASS
                            }`}
                    >
                        F5 TTS
                    </button>
                    <button
                        type="button"
                        onClick={() => setVoiceProvider('qwen')}
                        className={`${PROVIDER_TAB_CLASS} ${voiceProvider === 'qwen'
                            ? PROVIDER_TAB_ACTIVE_CLASS
                            : PROVIDER_TAB_INACTIVE_CLASS
                            }`}
                    >
                        Qwen 3 TTS
                    </button>
                </div>
            </div>

            <input
                ref={(el) => {
                    fileInputRef.current = el;
                    if (el) {
                        el.onchange = (e) => {
                            handleFileUpload(e as unknown as React.ChangeEvent<HTMLInputElement>);
                        };
                    }
                }}
                type="file"
                accept=".wav,.mp3,.flac,.ogg,.m4a,.aac,.wma,.aiff,.au"
                style={{ display: 'none', pointerEvents: 'auto' }}
            />

            {hasVoicesServiceError && (
                <div className="mb-6 bg-red-900/20 border border-red-500/50 rounded-lg p-4 flex items-start gap-3">
                    <AlertCircle className="h-5 w-5 text-red-400 flex-shrink-0 mt-0.5" />
                    <div className="flex-1">
                        <h3 className="text-red-300 font-semibold mb-1">Сервис голосов временно недоступен</h3>
                        <p className="text-red-200/80 text-sm">{voicesServiceErrorMessage}</p>
                    </div>
                    <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => {
                            queryClient.invalidateQueries({ queryKey: ['global-voices', voiceProvider] });
                            queryClient.invalidateQueries({ queryKey: ['user-voices', userId, voiceProvider] });
                            queryClient.invalidateQueries({ queryKey: ['enabled-voices', userId, voiceProvider] });
                        }}
                        className="border-red-400/50 text-red-200 hover:text-white hover:bg-red-500/20"
                    >
                        <RefreshCw className="h-4 w-4 mr-2" />
                        Повторить
                    </Button>
                </div>
            )}

            {!loading && whitelistStatus && whitelistStatus.can_manage_voices === false && (
                <div className="mb-6 bg-orange-900/20 border border-orange-500/50 rounded-lg p-4 flex items-start gap-3">
                    <Lock className="h-5 w-5 text-orange-400 flex-shrink-0 mt-0.5" />
                    <div className="flex-1">
                        <h3 className="text-orange-300 font-semibold mb-1">Вы не состоите в whitelist</h3>
                        <p className="text-orange-200/80 text-sm">
                            Для доступа к управлению голосами необходимо быть в белом списке (whitelist).
                            Обратитесь к администратору для получения доступа.
                        </p>
                        <p className="text-orange-200/60 text-xs mt-2">
                            [INFO] Вам доступна только базовая озвучка (gTTS) через основные настройки TTS.
                        </p>
                    </div>
                </div>
            )}



            {loading ? (
                <div className="col-span-full text-center py-12">
                    <p className="text-muted-foreground">Загрузка голосов...</p>
                </div>
            ) : !whitelistStatus ? (
                <div className="col-span-full">
                    <div className="text-center py-12 card-glass rounded-lg">
                        <AlertCircle className="h-16 w-16 mx-auto mb-4 text-slate-500" />
                        <p className="text-slate-300 text-lg mb-2 font-semibold">Не удалось определить доступ</p>
                        <p className="text-slate-400 text-sm mb-4">
                            Повторите обновление страницы или проверьте подключение к серверу.
                        </p>
                        <Button
                            variant="outline"
                            onClick={() => {
                                queryClient.invalidateQueries({ queryKey: ['global-voices', voiceProvider] });
                                queryClient.invalidateQueries({ queryKey: ['user-voices', userId, voiceProvider] });
                            }}
                        >
                            <RefreshCw className="h-4 w-4 mr-2" />
                            Обновить
                        </Button>
                    </div>
                </div>
            ) : whitelistStatus && whitelistStatus.can_manage_voices === false ? (
                <div className="col-span-full">
                    <div className="text-center py-12 card-glass rounded-lg">
                        <Lock className="h-16 w-16 mx-auto mb-4 text-orange-500" />
                        <p className="text-slate-300 text-lg mb-2 font-semibold">Вы не состоите в whitelist</p>
                        <p className="text-slate-400 text-sm mb-4">
                            Для доступа к управлению голосами необходимо быть в белом списке (whitelist)
                        </p>
                        <p className="text-slate-500 text-xs">
                            Обратитесь к администратору для получения доступа
                        </p>
                    </div>
                </div>
            ) : whitelistStatus?.can_manage_voices && globalVoices.length === 0 && userVoices.length === 0 ? (
                <div className="col-span-full">
                    <div className="text-center py-12">
                        <User className="h-16 w-16 mx-auto mb-4 text-slate-500" />
                        <p className="text-slate-300 text-lg mb-4">Загрузите свой первый голос</p>
                        <Dialog open={uploadDialogOpen} onOpenChange={(open) => {
                            setUploadDialogOpen(open);
                            if (!open) {
                                setUploadFile(null);
                                setVoiceName('');
                                setUploadReferenceText('');
                                if (fileInputRef.current) {
                                    fileInputRef.current.value = '';
                                }
                            }
                        }}>
                            <DialogTrigger asChild>
                                <Button className="bg-purple-600 hover:bg-purple-700">
                                    <Upload className="h-4 w-4 mr-2" />
                                    Загрузить свой голос
                                </Button>
                            </DialogTrigger>
                            <DialogContent
                                key="upload-dialog"
                                className="max-w-md"
                                onOpenAutoFocus={(e) => e.preventDefault()}
                                onCloseAutoFocus={(e) => e.preventDefault()}
                            >
                                <DialogHeader>
                                    <DialogTitle>Загрузка нового голоса</DialogTitle>
                                    <DialogDescription>
                                        Загрузите аудио файл для создания вашего голоса
                                    </DialogDescription>
                                </DialogHeader>
                                <div className="space-y-4 py-4">
                                    <div>
                                        <Label>Аудио файл (WAV, MP3, FLAC, OGG, M4A, AAC, WMA, AIFF, AU)</Label>
                                        <div className="mt-1">
                                            <Button
                                                type="button"
                                                variant="outline"
                                                onClick={(e) => {
                                                    e.preventDefault();
                                                    e.stopPropagation();
                                                    if (fileInputRef.current) {
                                                        fileInputRef.current.click();
                                                    }
                                                }}
                                                className="w-full"
                                            >
                                                <Upload className="h-4 w-4 mr-2" />
                                                {uploadFile ? uploadFile.name : 'Выбрать файл'}
                                            </Button>
                                        </div>
                                        {uploadFile && (
                                            <p className="mt-1 text-xs text-sky-400">
                                                Файл выбран: {uploadFile.name}
                                            </p>
                                        )}
                                    </div>
                                    <div>
                                        <Label htmlFor="voice-name">Имя голоса</Label>
                                        <Input
                                            id="voice-name"
                                            type="text"
                                            value={voiceName}
                                            onChange={(e) => setVoiceName(e.target.value)}
                                            placeholder="Введите имя голоса"
                                            className="mt-1"
                                        />
                                        <p className="text-xs text-slate-400 mt-1">
                                            Имя будет использоваться для выбора голоса в TTS
                                        </p>
                                    </div>
                                    <div>
                                        <Label htmlFor="voice-reference-text">Reference text</Label>
                                        <Textarea
                                            id="voice-reference-text"
                                            value={uploadReferenceText}
                                            onChange={(e) => setUploadReferenceText(e.target.value)}
                                            placeholder="Опционально. Если оставить пустым, backend попробует транскрибировать sample автоматически."
                                            className="mt-1 min-h-[96px]"
                                        />
                                        <p className="text-xs text-slate-400 mt-1">
                                            Для Qwen Base это reference_text для voice cloning. Для F5 поле тоже сохраняется вместе с sample.
                                        </p>
                                    </div>
                                    <div className="bg-blue-900/20 border border-blue-500/50 rounded-lg p-3">
                                        <p className="text-sm text-slate-300">
                                            Голос будет доступен только вам и загружен в вашу личную папку голосов.
                                        </p>
                                    </div>
                                </div>
                                <DialogFooter className="flex justify-center gap-4">
                                    <Button
                                        onClick={() => setUploadDialogOpen(false)}
                                        variant="outline"
                                        className="w-28"
                                    >
                                        Отмена
                                    </Button>
                                    <Button
                                        onClick={handleUpload}
                                        disabled={isUploading || !uploadFile || !voiceName.trim()}
                                        variant="ghost"
                                        className="w-36 border border-blue-500/30 bg-transparent text-blue-300 hover:bg-blue-500/10 hover:text-sky-300"
                                    >
                                        {isUploading ? 'Загрузка...' : 'Загрузить'}
                                    </Button>
                                </DialogFooter>
                            </DialogContent>
                        </Dialog>
                    </div>
                </div>
            ) : (
                <div className="space-y-8">
                    {whitelistStatus?.can_manage_voices && (
                        <div>
                            <div className="flex items-center justify-between mb-4">
                                <div className="flex items-center gap-2">
                                    <User className="h-5 w-5 text-sky-400" />
                                    <h3 className="text-lg font-semibold text-white">Мои голоса</h3>
                                    {userVoices.length > 0 && (
                                        <Badge variant="outline" className="border-sky-500/40 text-sky-400">
                                            {userVoices.length}
                                        </Badge>
                                    )}
                                </div>
                                <Dialog open={uploadDialogOpen} onOpenChange={(open) => {
                                    setUploadDialogOpen(open);
                                    if (!open) {
                                        setUploadFile(null);
                                        setVoiceName('');
                                        setUploadReferenceText('');
                                        if (fileInputRef.current) {
                                            fileInputRef.current.value = '';
                                        }
                                    }
                                    }}>
                                        <DialogTrigger asChild>
                                            <Button variant="outline" className="border-blue-700/40 bg-transparent text-muted-foreground hover:bg-transparent hover:text-blue-400">
                                                <Upload className="h-4 w-4 mr-2" />
                                                Загрузить свой голос
                                            </Button>
                                        </DialogTrigger>
                                    <DialogContent
                                        key="upload-dialog"
                                        className="max-w-md"
                                        onOpenAutoFocus={(e) => e.preventDefault()}
                                        onCloseAutoFocus={(e) => e.preventDefault()}
                                    >
                                        <DialogHeader>
                                            <DialogTitle>Загрузка нового голоса</DialogTitle>
                                            <DialogDescription>
                                                Загрузите аудио файл для создания вашего голоса
                                            </DialogDescription>
                                        </DialogHeader>
                                        <div className="space-y-4 py-4">
                                            <div>
                                                <Label>Аудио файл (WAV, MP3, FLAC, OGG, M4A, AAC, WMA, AIFF, AU)</Label>
                                                <div className="mt-1">
                                                    <Button
                                                        type="button"
                                                        variant="outline"
                                                        onClick={(e) => {
                                                            e.preventDefault();
                                                            e.stopPropagation();
                                                            if (fileInputRef.current) {
                                                                fileInputRef.current.click();
                                                            }
                                                        }}
                                                        className="w-full"
                                                    >
                                                        <Upload className="h-4 w-4 mr-2" />
                                                        {uploadFile ? uploadFile.name : 'Выбрать файл'}
                                                    </Button>
                                                </div>
                                                {uploadFile && <p className="mt-1 text-xs text-sky-400">Файл выбран: {uploadFile.name}</p>}
                                            </div>
                                            <div>
                                                <Label htmlFor="voice-name">Имя голоса</Label>
                                                <Input
                                                    id="voice-name"
                                                    type="text"
                                                    value={voiceName}
                                                    onChange={(e) => setVoiceName(e.target.value)}
                                                    placeholder="Введите имя голоса"
                                                    className="mt-1"
                                                />
                                                <p className="text-xs text-slate-400 mt-1">
                                                    Имя будет использоваться для выбора голоса в TTS
                                                </p>
                                            </div>
                                            <div>
                                                <Label htmlFor="voice-reference-text-secondary">Reference text</Label>
                                                <Textarea
                                                    id="voice-reference-text-secondary"
                                                    value={uploadReferenceText}
                                                    onChange={(e) => setUploadReferenceText(e.target.value)}
                                                    placeholder="Опционально. Если оставить пустым, backend попробует транскрибировать sample автоматически."
                                                    className="mt-1 min-h-[96px]"
                                                />
                                                <p className="text-xs text-slate-400 mt-1">
                                                    Для Qwen Base это reference_text для voice cloning. Для F5 поле тоже сохраняется вместе с sample.
                                                </p>
                                            </div>
                                            <div className="bg-blue-900/20 border border-blue-500/50 rounded-lg p-3">
                                                <p className="text-sm text-slate-300">
                                                    Голос будет доступен только вам и загружен в вашу личную папку голосов.
                                                </p>
                                            </div>
                                        </div>
                                        <DialogFooter className="flex justify-center gap-4">
                                            <Button
                                                onClick={() => setUploadDialogOpen(false)}
                                                variant="outline"
                                                className="w-28"
                                            >
                                                Отмена
                                            </Button>
                                            <Button
                                                onClick={handleUpload}
                                                disabled={isUploading || !uploadFile || !voiceName.trim()}
                                                variant="ghost"
                                                className="w-36 border border-blue-500/30 bg-transparent text-blue-300 hover:bg-blue-500/10 hover:text-sky-300"
                                            >
                                                {isUploading ? 'Загрузка...' : 'Загрузить'}
                                            </Button>
                                        </DialogFooter>
                                    </DialogContent>
                                </Dialog>
                            </div>
                            {userVoices.length === 0 ? (
                                <div className="rounded-2xl border border-border/70 border-dashed bg-card/55 py-8 text-center">
                                    <User className="mx-auto mb-3 h-10 w-10 text-slate-500" />
                                    <p className="mb-2 text-slate-300">У вас пока нет личных голосов</p>
                                    <p className="text-sm text-slate-500">Загрузите первый sample, чтобы начать</p>
                                </div>
                            ) : (
                                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
                                    {userVoices.map((voice) => renderVoiceCard(voice, 'user'))}
                                </div>
                            )}
                        </div>
                    )}

                    {whitelistStatus?.can_manage_voices && (
                        <div>
                            <div className="flex items-center gap-2 mb-4">
                                <Globe className="h-5 w-5 text-blue-400" />
                                <h3 className="text-lg font-semibold text-white">Глобальные голоса</h3>
                                <Badge variant="outline" className="border-sky-500/40 text-sky-400">
                                    {globalVoices.length}
                                </Badge>
                            </div>
                            {globalVoices.length === 0 ? (
                                <div className="rounded-2xl border border-border/70 border-dashed bg-card/55 py-12 text-center">
                                    <Globe className="h-16 w-16 mx-auto mb-4 text-slate-600" />
                                    <p className="text-slate-400 text-lg mb-2">Глобальных голосов пока нет</p>
                                    <p className="text-sm text-slate-500">Глобальные голоса доступны всем пользователям</p>
                                </div>
                            ) : (
                                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
                                    {globalVoices.map((voice) => renderVoiceCard(voice, 'global'))}
                                </div>
                            )}
                        </div>
                    )}
                </div>
            )}

            <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
                <DialogContent
                    key={`edit-dialog-${currentVoice?.id || 'new'}`}
                    className="max-w-lg"
                    onOpenAutoFocus={(e) => e.preventDefault()}
                    onCloseAutoFocus={(e) => e.preventDefault()}
                >
                    <DialogHeader>
                        <DialogTitle>{`Настройки голоса "${currentVoice?.name}"`}</DialogTitle>
                    </DialogHeader>
                    {currentVoice && (
                        <div className="space-y-4 py-4">
                            {currentVoice.voice_type === 'global' && (
                                <div className="bg-blue-900/20 border border-blue-500/50 rounded-lg p-2 flex items-center gap-2">
                                    <Lock className="h-4 w-4 text-blue-400 flex-shrink-0" />
                                    <p className="text-blue-200/80 text-xs">
                                        Настройки применяются только к вашему профилю
                                    </p>
                                </div>
                            )}

                            <div>
                                <Label htmlFor="reference-text">Референсный текст</Label>
                                <Textarea
                                    id="reference-text"
                                    value={currentVoice.reference_text || ''}
                                    onChange={(e) => handleReferenceTextChange(e.target.value)}
                                    className="mt-1 bg-slate-800"
                                    rows={3}
                                    placeholder="Введите референсный текст для синтеза..."
                                    disabled={currentVoice.voice_type === 'global'}
                                />
                                {currentVoice.voice_type === 'user' && (
                                    <div className="flex gap-2 mt-2 justify-end">
                                        <Button
                                            type="button"
                                            variant="outline"
                                            size="sm"
                                            onClick={handleTranscribe}
                                            disabled={isTranscribing}
                                            className="text-xs px-3"
                                        >
                                            {isTranscribing ? 'Транскрибирую...' : 'Перетранскрибировать'}
                                        </Button>
                                    </div>
                                )}
                            </div>

                            <div className="space-y-3">
                                <div className="flex items-center justify-between">
                                    <Label htmlFor="voice-volume">Индивидуальная громкость</Label>
                                    <span className="text-sm font-medium text-blue-300">
                                        {voiceVolumes[currentVoice.name] || 50}%
                                    </span>
                                </div>
                                <div className="space-y-2">
                                    <Slider
                                        id="voice-volume"
                                        min={0}
                                        max={100}
                                        step={1}
                                        value={[voiceVolumes[currentVoice.name] || 50]}
                                        onValueChange={(value) => {
                                            const newVolume = value[0];
                                            if (voiceVolumeSaveTimeout.current[currentVoice.name]) {
                                                clearTimeout(voiceVolumeSaveTimeout.current[currentVoice.name]);
                                            }
                                            voiceVolumeSaveTimeout.current[currentVoice.name] = setTimeout(() => {
                                                saveVoiceVolume(currentVoice.name, newVolume);
                                            }, 500);
                                        }}
                                        className="w-full"
                                    />
                                </div>
                            </div>

                            <div>
                                <Label htmlFor="test-text">Текст для тестирования</Label>
                                <Textarea
                                    id="test-text"
                                    value={testText}
                                    onChange={(e) => setTestText(e.target.value)}
                                    className="mt-1"
                                    rows={3}
                                />
                            </div>

                            <div className="space-y-4">
                                <h4 className="text-sm font-medium text-white">Настройки генерации</h4>
                            {voiceProvider === 'f5' ? (
                                <div className="space-y-4">
                                    <div>
                                        <Label htmlFor="cfg-strength">Стабильность синтеза: {currentVoice.cfg_strength}</Label>
                                        <Slider
                                            id="cfg-strength"
                                            min={0.1}
                                            max={10.0}
                                            step={0.1}
                                            value={[currentVoice.cfg_strength || 3.0]}
                                            onValueChange={(value) => setCurrentVoice(prev => prev ? ({ ...prev, cfg_strength: value[0] }) : null)}
                                            className="mt-2"
                                        />
                                    </div>

                                    <div>
                                        <Label htmlFor="speed-preset">Скорость речи: {
                                            currentVoice.speed_preset === 'very_slow' ? 'Очень медленный' :
                                                currentVoice.speed_preset === 'slow' ? 'Медленный' :
                                                    currentVoice.speed_preset === 'normal' ? 'Нормальный' :
                                                        currentVoice.speed_preset === 'fast' ? 'Быстрый' : 'Очень быстрый'
                                        }</Label>
                                        <Slider
                                            id="speed-preset"
                                            min={0}
                                            max={4}
                                            step={1}
                                            value={[
                                                currentVoice.speed_preset === 'very_slow' ? 0 :
                                                    currentVoice.speed_preset === 'slow' ? 1 :
                                                        currentVoice.speed_preset === 'normal' ? 2 :
                                                            currentVoice.speed_preset === 'fast' ? 3 : 4
                                            ]}
                                            onValueChange={(value) => {
                                                const preset = value[0] === 0 ? 'very_slow' :
                                                    value[0] === 1 ? 'slow' :
                                                        value[0] === 2 ? 'normal' :
                                                            value[0] === 3 ? 'fast' : 'very_fast';
                                                logger.log('Speed preset changed to:', preset);
                                                setCurrentVoice(prev => prev ? ({ ...prev, speed_preset: preset }) : null);
                                            }}
                                            className="mt-2"
                                        />
                                        <div className="mt-1 grid grid-cols-5 gap-1 px-1 text-[11px] text-muted-foreground">
                                            <span className="text-center whitespace-nowrap leading-none">Очень медл.</span>
                                            <span className="text-center whitespace-nowrap leading-none">Медленный</span>
                                            <span className="text-center whitespace-nowrap leading-none">Нормальный</span>
                                            <span className="text-center whitespace-nowrap leading-none">Быстрый</span>
                                            <span className="text-center whitespace-nowrap leading-none">Очень быстр.</span>
                                        </div>
                                    </div>
                                </div>
                            ) : (
                                <div className="rounded-xl border border-border/70 bg-background/50 px-4 py-3 text-sm text-muted-foreground">
                                    Для Qwen здесь используются только reference text, sample и индивидуальная громкость. Генерационный промпт модели задаётся на основной вкладке TTS.
                                </div>
                            )}
                            </div>
                        </div>
                    )}
                    <DialogFooter className="flex-wrap gap-2">
                        <Button onClick={handleTestVoice} variant="outline" disabled={isTestingVoice} className="flex-1 min-w-[clamp(92px,18vw,120px)] whitespace-nowrap">
                            <TestTube2 className="h-4 w-4 mr-2" />{isTestingVoice ? 'Генерирую...' : 'Тест'}
                        </Button>
                        {currentVoice?.voice_type === 'user' && (
                            <Button onClick={handleRenameVoice} variant="outline" className="flex-1 min-w-[clamp(120px,22vw,160px)] whitespace-nowrap text-orange-600 border-orange-600 hover:bg-orange-600 hover:text-white">
                                <Edit className="h-4 w-4 mr-2" />Переименовать
                            </Button>
                        )}
                        <Button onClick={handleSaveSettings} className="flex-1 min-w-[clamp(110px,20vw,140px)] whitespace-nowrap bg-blue-700 hover:bg-blue-800">
                            <Settings className="h-4 w-4 mr-2" />Сохранить
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            <Dialog open={renameDialogOpen} onOpenChange={setRenameDialogOpen}>
                <DialogContent
                    key="rename-dialog"
                    className="max-w-md"
                    onOpenAutoFocus={(e) => e.preventDefault()}
                    onCloseAutoFocus={(e) => e.preventDefault()}
                >
                    <DialogHeader>
                        <DialogTitle>Переименовать голос</DialogTitle>
                        <DialogDescription>
                            Введите новое имя для голоса "{currentVoice?.name}"
                        </DialogDescription>
                    </DialogHeader>
                    <div className="space-y-4 py-4">
                        <div>
                            <Label htmlFor="new-voice-name">Новое имя</Label>
                            <Input
                                id="new-voice-name"
                                value={newVoiceName}
                                onChange={(e) => setNewVoiceName(e.target.value)}
                                placeholder="Введите новое имя голоса..."
                                className="mt-1"
                                onKeyDown={(e) => {
                                    if (e.key === 'Enter') {
                                        handleConfirmRename();
                                    }
                                }}
                            />
                        </div>
                    </div>
                    <DialogFooter>
                        <Button
                            variant="outline"
                            onClick={() => setRenameDialogOpen(false)}
                        >
                            Отмена
                        </Button>
                        <Button
                            onClick={handleConfirmRename}
                            disabled={!newVoiceName.trim() || newVoiceName.trim() === currentVoice?.name}
                        >
                            Переименовать
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </PageWrapper>
    );
};

const VoiceManagementPage: React.FC = () => {
    return <VoiceManagementPageContent />;
};

export default VoiceManagementPage;




